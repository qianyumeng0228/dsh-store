# -*- coding: utf-8 -*-
"""incremental.py —— 增量管道。
1. --commit-only：给 git-only 插件补默认分支最新 commit（GitHub API）
   无 token 时受 60 req/h 限制，每轮只补最活跃的 N 个；有 token 补 5,000 上限。
2. --scan-new：扫 GitHub topic:dsh-plugin 新仓库，探测 package.json 的 dsh 字段，入库。
3. --all：先补 commit，再扫新仓库。

用法:
    python pipeline/incremental.py --commit-only
    python pipeline/incremental.py --scan-new
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
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
NPM_DL_API = "https://api.npmjs.org/downloads/point/last-month"


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit-only", action="store_true")
    ap.add_argument("--scan-new", action="store_true")
    ap.add_argument("--downloads", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    git_pending = conn.execute(
        "SELECT COUNT(*) FROM plugins WHERE install_source='git' AND (git_commit IS NULL OR git_commit='')"
    ).fetchone()[0]
    print(f"[incremental] 库内 git 待补 commit: {git_pending}")

    if args.commit_only or args.all:
        limit = config.GIT_COMMIT_BATCH_TOKEN if config.GITHUB_TOKEN else config.GIT_COMMIT_BATCH_NO_TOKEN
        fill_commits(conn, limit)
    if args.downloads or args.all:
        fill_downloads(conn, config.NPM_DL_BATCH)
    if args.scan_new or args.all:
        scan_new(conn)
    conn.close()


if __name__ == "__main__":
    main()
