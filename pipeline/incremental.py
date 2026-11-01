# -*- coding: utf-8 -*-
"""incremental.py —— 增量管道。
1. --commit-only：给 git-only 插件补默认分支最新 commit（GitHub API）
   无 token 时受 60 req/h 限制，每轮只补最活跃的 N 个；有 token 补 5,000 上限。
2. --scan-new：扫 GitHub topic:dsh-plugin 新仓库，探测 package.json 的 dsh 字段，入库。
3. --downloads：npm 插件近 30 天下载量（api.npmjs.org）。
4. --updates：更新检测
   - npm：registry.npmjs.org/{pkg}/latest 刷新 npm_meta（latest/latest_since/checked），
     同时解析 repository.url 存 npm_repo、engines.dsh 存 dsh_engines；
     latest 与收录版差异满 7 天则把 npm_version 追平为 latest。
   - git：按 stars 降序批量刷新 git_latest_commit；git_checked 记录差异开始日，
     差异满 7 天则把 git_commit 追平。
5. --icons：按 stars 降序批量探测仓库常见图标路径（raw.githubusercontent HEAD），
   命中存 icon_probe.icon_path，7 天内已探测的跳过。
6. --all：commit-only + downloads + updates + icons + scan-new。

用法:
    python pipeline/incremental.py --commit-only
    python pipeline/incremental.py --scan-new
    python pipeline/incremental.py --updates --icons
    python pipeline/incremental.py --all
环境变量 GITHUB_TOKEN 可选（推荐提供，速度差两个数量级）。
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from db import ensure_schema, parse_github_repo

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
NPM_DL_API = "https://api.npmjs.org/downloads/point/last-month"
NPM_REGISTRY = "https://registry.npmjs.org"
NPM_MIRROR = "https://registry.npmmirror.com"

# 图标探测候选路径（raw.githubusercontent.com/{repo}/HEAD/{path}）
ICON_CANDIDATES = [
    "assets/icon.png", "assets/logo.png",
    "docs/icon.png", "docs/logo.png",
    "icon.png", "logo.png",
    "assets/icons/icon.png", "static/icon.png",
]


def api_get(url, timeout=20):
    """GET GitHub API，自动处理限速。返回 (json, rate_remaining) 或 (None, remaining)。"""
    req = Request(url, headers=config.GITHUB_UA)
    try:
        with urlopen(req, timeout=timeout) as r:
            remaining = r.headers.get("X-RateLimit-Remaining")
            raw = r.read().decode("utf-8-sig")  # utf-8-sig 兼容带 BOM 的 package.json
            try:
                return json.loads(raw), remaining
            except json.JSONDecodeError:
                print(f"[api] 非 JSON 响应: {url[:80]} ({len(raw)} bytes)")
                return None, remaining
    except HTTPError as e:
        if e.code == 403:
            # 限速或 token 失效
            body = e.read().decode("utf-8", "ignore")
            if "rate limit" in body.lower() or "rate_limit" in body.lower():
                print(f"[api] 限速: {url[:80]} (HTTP 403)")
                time.sleep(60)
                return None, "0"
            print(f"[api] 403: {url[:80]} {body[:200]}")
            return None, "0"
        if e.code == 404:
            return None, None
        print(f"[api] HTTP {e.code}: {url[:100]}")
        return None, None
    except URLError as e:
        print(f"[api] 网络错误: {e.reason} ({url[:80]})")
        time.sleep(5)
        return None, None


def repo_commit(repo: str):
    """拿仓库默认分支最新 commit sha。repo = owner/name。"""
    data, _ = api_get(f"{config.GITHUB_API}/repos/{urllib.parse.quote(repo)}/commits?per_page=1")
    if isinstance(data, list) and data:
        return data[0].get("sha")
    return None


def probe_repo(repo: str):
    """探测仓库是否是 DSH 插件：找 package.json 的 dsh 字段。
    返回 (evidence, npm) 或 (None, None)。按成本从低到高探测路径。"""
    paths = ["package.json", "plugin/package.json", "dsh/package.json",
             "dsh-plugin/package.json", "packages/dsh-plugin/package.json"]
    for p in paths:
        data, _ = api_get(f"https://raw.githubusercontent.com/{repo}/HEAD/{p}")
        if not isinstance(data, dict):
            continue
        if "dsh" in data or "dsh.bundle" in data:
            ev = f"{p}#dsh"
            npm = data.get("name") if isinstance(data.get("name"), str) else None
            return ev, npm
        deps = data.get("dependencies") or data.get("peerDependencies") or {}
        if deps.get("@deepseek-ai/dsh") or deps.get("@deepseek-ai/dsh-tools") or deps.get("@deepseek-ai/dsh-agent"):
            return f"{p}#dependencies", data.get("name")
    return None, None


def fill_commits(conn, limit):
    """给 git-only 插件补 commit。按 stars 降序取 limit 个。"""
    rows = conn.execute("""
        SELECT name, repo FROM plugins
        WHERE install_source='git' AND (git_commit IS NULL OR git_commit='')
        ORDER BY stars DESC LIMIT ?
    """, (limit,)).fetchall()
    print(f"[commit] 待补 {limit} 个（按 stars 降序）")
    ok = fail = 0
    for name, repo in rows:
        sha = repo_commit(repo)
        if sha:
            conn.execute("UPDATE plugins SET git_commit=? WHERE name=?", (sha, name))
            ok += 1
        else:
            fail += 1
        conn.commit()
        time.sleep(0.2)  # 轻微限速保护
    print(f"[commit] 完成: 成功 {ok} / 失败 {fail}")
    return ok


def scan_new(conn, pages=3):
    """扫 topic:dsh-plugin 最近更新的仓库，新仓库入库。"""
    added = 0
    for page in range(1, pages + 1):
        q = urllib.parse.quote("topic:dsh-plugin")
        data, _ = api_get(
            f"{config.GITHUB_API}/search/repositories?q={q}&sort=updated&order=desc&per_page=100&page={page}")
        if not isinstance(data, dict):
            print(f"[scan] 第 {page} 页失败，停止")
            break
        items = data.get("items", [])
        if not items:
            break
        for it in items:
            full = it.get("full_name", "")
            if not full:
                continue
            exists = conn.execute("SELECT 1 FROM plugins WHERE repo=?", (full,)).fetchone()
            if exists:
                continue
            ev, npm = probe_repo(full)
            if not ev:
                print(f"[scan] 跳过（非插件）: {full}")
                continue
            desc = (it.get("description") or "")[:2000]
            has_cjk = bool(CJK_RE.search(desc))
            cat = "plugin"
            tags = ["dsh-plugin"]
            for t in (it.get("topics") or [])[:20]:
                tags.append(str(t))
            stars = int(it.get("stargazers_count") or 0)
            pushed = (it.get("pushed_at") or "")[:10]
            created = (it.get("created_at") or "")[:10]
            conn.execute("""
                INSERT OR IGNORE INTO plugins
                (name, repo, description, category, official, added, status, evidence,
                 npm, stars, stars_updated, pushed_at, tags, title_en, title_zh,
                 desc_en, desc_zh, install_source, category_key, last_seen)
                VALUES (?,?,?,?,0,?, 'verified', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'tools', ?)
            """, (
                full.split("/")[-1], full, desc, cat, created, ev, npm or None,
                stars, it.get("pushed_at") or "", pushed,
                json.dumps(tags, ensure_ascii=False),
                full.split("/")[-1].replace("dsh-", "").title(),
                full.split("/")[-1], desc if not has_cjk else "", desc if has_cjk else "",
                "npm" if npm else "git", it.get("pushed_at") or "",
            ))
            added += 1
            print(f"[scan] 新增: {full} (stars={stars}, source={'npm' if npm else 'git'})")
        conn.commit()
        time.sleep(2)  # search API 限速
    print(f"[scan] 新增 {added} 个")
    return added


def npm_downloads(pkg: str):
    """npm 包近 30 天下载量；404 快速失败，其余异常重试 2 次，仍失败返回 None。"""
    last_exc = None
    for attempt in range(3):
        try:
            req = Request(f"{NPM_DL_API}/{urllib.parse.quote(pkg, safe='')}",
                          headers={"User-Agent": "dsh-store/0.1", "Accept": "application/json"})
            with urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
            n = data.get("downloads")
            return int(n) if n is not None else None
        except HTTPError as e:
            if e.code == 404:
                return None  # 包不存在，无需重试
            last_exc = e
            if attempt < 2:
                time.sleep(1 + attempt)
        except Exception as e:
            last_exc = e
            if attempt < 2:
                time.sleep(1 + attempt)
    return None


def fill_downloads(conn, limit):
    """给 npm 插件补近 30 天下载量（npm_downloads 表，按 stars 降序，多线程）。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS npm_downloads (
            pkg TEXT PRIMARY KEY, downloads INTEGER, updated TEXT
        )
    """)
    conn.commit()
    rows = conn.execute("""
        SELECT p.name, p.npm, p.stars FROM plugins p
        WHERE p.install_source='npm' AND p.npm IS NOT NULL AND p.npm!=''
          AND NOT EXISTS (SELECT 1 FROM npm_downloads d WHERE d.pkg=p.npm)
        ORDER BY p.stars DESC LIMIT ?
    """, (limit,)).fetchall()
    print(f"[downloads] 待补 {len(rows)} 个（近30天下载量）")
    if not rows:
        return 0
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(npm_downloads, npm): (name, npm) for name, npm, _ in rows}
        done = 0
        for fut in as_completed(futs):
            name, npm = futs[fut]
            n = fut.result()
            done += 1
            if n is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO npm_downloads(pkg, downloads, updated) VALUES (?,?,?)",
                    (npm, n, time.strftime("%Y-%m-%d")))
                ok += 1
            else:
                fail += 1
            if done % 200 == 0:
                conn.commit()
                print(f"[downloads] 进度 {done}/{len(rows)}")
    conn.commit()
    print(f"[downloads] 完成: 成功 {ok} / 失败 {fail}")
    return ok


# ---------------------------------------------------------------- updates ----

def _today():
    return time.strftime("%Y-%m-%d")


def _days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _registry_get_json(url, timeout=20, retries=2):
    """registry 请求：官方源失败（429/5xx/超时/连接）自动降级 npmmirror 镜像，
    每源在 retries+1 轮里各尝试一次；404 直接抛；全部失败抛最后异常。"""
    mirror = url.replace(NPM_REGISTRY, NPM_MIRROR, 1) if url.startswith(NPM_REGISTRY) else None
    last = None
    for attempt in range(retries + 1):
        for src in ([url] + ([mirror] if mirror else [])):
            try:
                req = Request(src, headers={"User-Agent": "dsh-store/0.1",
                                            "Accept": "application/json"})
                with urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except HTTPError as e:
                last = e
                if e.code == 404:
                    raise
                continue
            except Exception as e:
                last = e
                continue
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise last


def npm_latest_full(pkg: str):
    """registry.npmjs.org/{pkg}/latest（失败自动走 npmmirror）。
    返回 dict {latest, repo, engines_dsh}；404 返回 {latest: None}；重试耗尽返回 None。"""
    url = f"{NPM_REGISTRY}/{urllib.parse.quote(pkg, safe='@/')}/latest"
    try:
        data = _registry_get_json(url)
    except HTTPError as e:
        if e.code == 404:
            return {"latest": None, "repo": "", "engines_dsh": None}
        return None
    except Exception:
        return None
    if not data:
        return {"latest": None, "repo": "", "engines_dsh": None}
    repo_url = ""
    repo_field = data.get("repository")
    if isinstance(repo_field, dict):
        repo_url = repo_field.get("url") or ""
    elif isinstance(repo_field, str):
        repo_url = repo_field
    eng = (data.get("engines") or {}).get("dsh")
    return {"latest": data.get("version"),
            "repo": parse_github_repo(repo_url),
            "engines_dsh": eng}


def updates_npm(conn):
    """npm 更新检测：刷新 npm_meta + npm_repo + dsh_engines；差异满 7 天追平 npm_version。"""
    ensure_schema(conn)
    today = _today()
    cutoff_7 = _days_ago(7)
    rows = conn.execute("""
        SELECT name, npm FROM plugins
        WHERE install_source='npm' AND npm IS NOT NULL AND npm!=''
        ORDER BY stars DESC
    """).fetchall()
    print(f"[updates-npm] 待刷新 {len(rows)} 个 npm 包")
    if not rows:
        return 0

    results = []
    fails = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(npm_latest_full, npm): (name, npm) for name, npm in rows}
        done = 0
        for fut in as_completed(futs):
            name, npm = futs[fut]
            res = fut.result()
            done += 1
            if res is None:
                fails += 1
            else:
                results.append((name, npm, res))
            if done % 1000 == 0:
                print(f"[updates-npm] 进度 {done}/{len(rows)}")
    total = len(rows)
    if total and fails / total > 0.3:
        print(f"[updates-npm] 失败率 {fails}/{total} >30%，本轮中断，保留旧数据")
        return 0

    n_meta = n_repo = n_eng = n_catchup = 0
    for name, npm, res in results:
        latest = res.get("latest")
        prev = conn.execute(
            "SELECT latest, latest_since FROM npm_meta WHERE pkg=?", (npm,)).fetchone()
        if prev is None:
            since = today
            conn.execute(
                "INSERT INTO npm_meta(pkg,latest,latest_since,checked) VALUES(?,?,?,?)",
                (npm, latest, since, today))
        else:
            prev_latest, prev_since = prev
            since = today if latest != prev_latest else (prev_since or today)
            conn.execute(
                "UPDATE npm_meta SET latest=?, latest_since=?, checked=? WHERE pkg=?",
                (latest, since, today, npm))
        n_meta += 1
        if res.get("repo"):
            conn.execute(
                "UPDATE plugins SET npm_repo=? WHERE name=? AND (npm_repo IS NULL OR npm_repo='')",
                (res["repo"], name))
            n_repo += 1
        if res.get("engines_dsh") is not None:
            conn.execute("UPDATE plugins SET dsh_engines=? WHERE name=?",
                         (res["engines_dsh"], name))
            n_eng += 1
        # 收录版本追平：latest != 收录版本 且 latest_since 距今 >= 7 天
        if latest:
            nv = conn.execute(
                "SELECT npm_version FROM plugins WHERE name=?", (name,)).fetchone()
            nv = nv[0] if nv else None
            if nv and latest != nv and since <= cutoff_7:
                conn.execute("UPDATE plugins SET npm_version=? WHERE name=?",
                             (latest, name))
                n_catchup += 1
    conn.commit()
    print(f"[updates-npm] 完成: npm_meta {n_meta} / 解析仓库 {n_repo} / engines.dsh {n_eng} / 追平 {n_catchup}")
    return n_meta


def updates_git(conn, batch):
    """git 更新检测：批量刷新 git_latest_commit；差异满 7 天追平 git_commit。"""
    ensure_schema(conn)
    today = _today()
    cutoff_7 = _days_ago(7)
    rows = conn.execute("""
        SELECT name, repo, git_commit FROM plugins
        WHERE install_source='git' AND repo IS NOT NULL AND repo!=''
          AND (git_checked IS NULL OR git_checked='' OR git_checked <= ?)
        ORDER BY stars DESC LIMIT ?
    """, (cutoff_7, batch)).fetchall()
    print(f"[updates-git] 待刷新 {len(rows)} 个仓库（batch={batch}）")
    if not rows:
        return 0

    def work(item):
        nm, rp, gc = item
        sha = repo_commit(rp)
        return nm, rp, gc, sha

    n_newdiff = n_catchup = n_nodiff = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, r): r for r in rows}
        done = 0
        for fut in as_completed(futs):
            nm, rp, gc, sha = fut.result()
            done += 1
            if not sha:
                continue
            if not gc:
                conn.execute("UPDATE plugins SET git_latest_commit=? WHERE name=?", (sha, nm))
            elif sha != gc:
                # 发现差异：git_checked 为空 -> 置今天；已满 7 天 -> 追平
                old_checked = conn.execute(
                    "SELECT git_checked FROM plugins WHERE name=?", (nm,)).fetchone()
                old_checked = old_checked[0] if old_checked else ""
                if not old_checked:
                    conn.execute(
                        "UPDATE plugins SET git_latest_commit=?, git_checked=? WHERE name=?",
                        (sha, today, nm))
                    n_newdiff += 1
                elif old_checked <= cutoff_7:
                    conn.execute(
                        "UPDATE plugins SET git_latest_commit=?, git_commit=?, git_checked='' WHERE name=?",
                        (sha, sha, nm))
                    n_catchup += 1
                # 差异未满 7 天的本轮已被 SQL 过滤掉，不会落到这里
            else:
                # 无差异：记录最新 commit，清空 git_checked
                conn.execute(
                    "UPDATE plugins SET git_latest_commit=?, git_checked='' WHERE name=?",
                    (sha, nm))
                n_nodiff += 1
            if done % 200 == 0:
                conn.commit()
                print(f"[updates-git] 进度 {done}/{len(rows)}")
    conn.commit()
    print(f"[updates-git] 完成: 新差异 {n_newdiff} / 追平 {n_catchup} / 无差异 {n_nodiff}")
    return done


# ------------------------------------------------------------------ icons ----

def probe_icon(repo: str):
    """HEAD 探测仓库图标路径，命中返回相对路径，没找到返回 ''。"""
    for path in ICON_CANDIDATES:
        try:
            req = Request(f"https://raw.githubusercontent.com/{repo}/HEAD/{path}",
                          method="HEAD", headers={"User-Agent": "dsh-store/0.1"})
            with urlopen(req, timeout=8) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                if r.status == 200 and ct.startswith("image/"):
                    return path
        except HTTPError as e:
            if e.code in (404, 403):
                continue
            continue
        except Exception:
            continue
    return ""


def icons_probe(conn, batch):
    """按 stars 降序批量探测仓库图标，结果写 icon_probe。"""
    ensure_schema(conn)
    today = _today()
    cutoff_7 = _days_ago(7)
    rows = conn.execute("""
        SELECT repo FROM (
            SELECT repo, MAX(stars) AS st FROM plugins
            WHERE repo IS NOT NULL AND repo!='' GROUP BY repo
            UNION
            SELECT npm_repo AS repo, MAX(stars) AS st FROM plugins
            WHERE npm_repo IS NOT NULL AND npm_repo!='' GROUP BY npm_repo
        )
        WHERE NOT EXISTS (
            SELECT 1 FROM icon_probe ip WHERE ip.repo = repo AND ip.checked > ?
        )
        ORDER BY st DESC LIMIT ?
    """, (cutoff_7, batch)).fetchall()
    print(f"[icons] 待探测 {len(rows)} 个仓库（batch={batch}）")
    if not rows:
        return 0

    hits = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(probe_icon, r[0]): r[0] for r in rows}
        done = 0
        for fut in as_completed(futs):
            repo = futs[fut]
            path = fut.result()
            done += 1
            conn.execute(
                "INSERT OR REPLACE INTO icon_probe(repo, icon_path, checked) VALUES(?,?,?)",
                (repo, path, today))
            if path:
                hits += 1
            if done % 500 == 0:
                conn.commit()
                print(f"[icons] 进度 {done}/{len(rows)}，命中 {hits}")
    conn.commit()
    print(f"[icons] 完成: 探测 {done} / 命中 {hits}")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit-only", action="store_true")
    ap.add_argument("--scan-new", action="store_true")
    ap.add_argument("--downloads", action="store_true")
    ap.add_argument("--updates", action="store_true", help="npm + git 更新检测")
    ap.add_argument("--icons", action="store_true", help="仓库图标路径探测")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--git-batch", type=int, default=config.GIT_UPDATE_BATCH,
                    help="git 更新检测每轮仓库数（默认 config.GIT_UPDATE_BATCH）")
    ap.add_argument("--icon-batch", type=int, default=config.ICON_BATCH,
                    help="图标探测每轮仓库数（默认 config.ICON_BATCH）")
    args = ap.parse_args()

    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    ensure_schema(conn)
    git_pending = conn.execute(
        "SELECT COUNT(*) FROM plugins WHERE install_source='git' AND (git_commit IS NULL OR git_commit='')"
    ).fetchone()[0]
    print(f"[incremental] 库内 git 待补 commit: {git_pending}")

    if args.commit_only or args.all:
        limit = config.GIT_COMMIT_BATCH_TOKEN if config.GITHUB_TOKEN else config.GIT_COMMIT_BATCH_NO_TOKEN
        fill_commits(conn, limit)
    if args.downloads or args.all:
        fill_downloads(conn, config.NPM_DL_BATCH)
    if args.updates or args.all:
        updates_npm(conn)
        updates_git(conn, args.git_batch)
    if args.icons or args.all:
        icons_probe(conn, args.icon_batch)
    if args.scan_new or args.all:
        scan_new(conn)
    conn.close()


if __name__ == "__main__":
    main()
