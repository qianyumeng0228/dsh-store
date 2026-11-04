# -*- coding: utf-8 -*-
"""incremental.py —— 增量管道。
1. --commit-only：给 git-only 插件补默认分支最新 commit（GitHub API）
   无 token 时受 60 req/h 限制，每轮只补最活跃的 N 个；有 token 补 5,000 上限。
2. --scan-new：扫 GitHub topic 查询表（config.GIT_TOPIC_QUERIES）新仓库，
   探测 package.json 的 dsh 字段，入库。
3. --downloads：npm 插件近 30 天下载量（api.npmjs.org）。
4. --updates：更新检测
   - npm：registry.npmjs.org/{pkg}/latest 刷新 npm_meta（latest/latest_since/checked），
     同时解析 repository.url 存 npm_repo、engines.dsh 存 dsh_engines；
     latest 与收录版差异满 7 天则把 npm_version 追平为 latest。
     按 stars 降序轮转，每轮 --npm-batch 个（7 天内已刷过的跳过）。
   - git：先用一条 SQL 把满 7 天的已知差异追平（不花 API 配额），再按 stars 降序轮转
     探测默认分支 HEAD，每轮 --git-batch 个，7 天内探测过的跳过（节流看 git_probed，
     与差异计时 git_checked 分列，否则无差异的高星仓库每轮占满 batch、长尾饿死）。
5. --icons：按 stars 降序批量探测仓库常见图标路径（raw.githubusercontent HEAD），
   命中存 icon_probe.icon_path，7 天内已探测的跳过。
6. --gitver：从**锁定的 commit** 读 package.json#version 存 git_version（raw 请求，
   不占 API 限额）。git_ver_commit 记下读自哪个 commit，commit 没变不重读。
   build 据此填索引里 git 条目的 version，不再恒为占位 0.0.0。
7. --discover：四路发现（见 discover.py）——外部索引地址、GitHub code search、
   npm 关键词爬取，新增一律先落 status='pending' 候选，再读 package.json 判真。
8. --all：discover（含 scan-new）+ commit-only + downloads + updates + icons + gitver。
   发现排在最前，本轮新收的 git 候选才能立刻进补 commit 流程。

用法:
    python pipeline/incremental.py --commit-only
    python pipeline/incremental.py --scan-new
    python pipeline/incremental.py --discover
    python pipeline/incremental.py --updates --icons
    python pipeline/incremental.py --gitver --gitver-batch 300
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
    except Exception as e:
        # RemoteDisconnected / 连接重置这类不是 URLError 的子类，此前会一路冒泡
        # 炸掉整个 incremental 步骤，导致 build 与 Pages 部署都不执行。
        # 单个仓库本轮跳过即可：git_commit 仍为 NULL，下一轮会重新排到。
        print(f"[api] 连接异常 {type(e).__name__}: {str(e)[:80]} ({url[:80]})")
        time.sleep(2)
        return None, None


def repo_commit(repo: str):
    """拿仓库默认分支最新 commit sha。repo = owner/name。"""
    data, _ = api_get(f"{config.GITHUB_API}/repos/{urllib.parse.quote(repo)}/commits?per_page=1")
    if isinstance(data, list) and data:
        return data[0].get("sha")
    return None


def probe_repo(repo: str):
    """探测仓库是否是 DSH 插件：找 package.json 的 dsh 字段。
    返回 (evidence, npm, subdir, is_bundle)；不是插件返回 (None, None, "", False)。
    按成本从低到高探测路径。命中的子目录必须回传并落库，
    否则 monorepo 会被装到仓库根（索引里 source.path 就靠它）。"""
    paths = ["package.json", "plugin/package.json", "dsh/package.json",
             "dsh-plugin/package.json", "packages/dsh-plugin/package.json"]
    for p in paths:
        data, _ = api_get(f"https://raw.githubusercontent.com/{repo}/HEAD/{p}")
        if not isinstance(data, dict):
            continue
        subdir = p[: -len("package.json")].rstrip("/")
        dsh = data.get("dsh")
        # bundle 判定：dsh.bundle 存在。旧实现写的是 "dsh.bundle" in data，
        # 那只匹配字面带点的键名，永远命中不了
        is_bundle = (isinstance(dsh, dict) and "bundle" in dsh) or ("dsh.bundle" in data)
        if dsh is not None or is_bundle:
            ev = f"{p}#dsh" + ("bundle" if is_bundle else "")
            npm = data.get("name") if isinstance(data.get("name"), str) else None
            return ev, npm, subdir, is_bundle
        deps = data.get("dependencies") or data.get("peerDependencies") or {}
        if isinstance(deps, dict) and (deps.get("@deepseek-ai/dsh") or deps.get("@deepseek-ai/dsh-tools")
                                       or deps.get("@deepseek-ai/dsh-agent")):
            npm = data.get("name") if isinstance(data.get("name"), str) else None
            return f"{p}#dependencies", npm, subdir, False
    return None, None, "", False


def fill_commits(conn, limit):
    """给 git-only 插件补 commit。按 stars 降序取 limit 个。

    失败（删库/改名/私有 → 404）要落死信标记：此前失败什么都不写，这些仓库每轮
    都会按 stars 重新排进名额里白跑一遍，越到长尾浪费越重（实测抽样里 1/4 是死库）。
    """
    cutoff = _days_ago(config.GIT_DEAD_RETRY_DAYS)
    rows = conn.execute("""
        SELECT name, repo FROM plugins
        WHERE install_source='git' AND (git_commit IS NULL OR git_commit='')
          AND (git_dead_at IS NULL OR git_dead_at='' OR git_dead_at < ?)
        ORDER BY stars DESC LIMIT ?
    """, (cutoff, limit)).fetchall()
    dead_held = conn.execute(
        "SELECT COUNT(*) FROM plugins WHERE git_dead_at IS NOT NULL AND git_dead_at!='' "
        "AND git_dead_at >= ? AND (git_commit IS NULL OR git_commit='')", (cutoff,)).fetchone()[0]
    print(f"[commit] 待补 {limit} 个（按 stars 降序；另有 {dead_held} 个死仓库在冷却期内被跳过）")
    ok = fail = 0
    for name, repo in rows:
        sha = repo_commit(repo)
        if sha:
            conn.execute("UPDATE plugins SET git_commit=?, git_dead_at='', git_fail_count=0 "
                         "WHERE name=?", (sha, name))
            ok += 1
        else:
            fails = (conn.execute("SELECT git_fail_count FROM plugins WHERE name=?",
                                  (name,)).fetchone() or (0,))[0] or 0
            fails += 1
            dead = _today() if fails >= config.GIT_DEAD_MAX_FAILS else ""
            conn.execute("UPDATE plugins SET git_fail_count=?, git_dead_at=? WHERE name=?",
                         (fails, dead, name))
            fail += 1
        conn.commit()
        time.sleep(0.2)  # 轻微限速保护
    print(f"[commit] 完成: 成功 {ok} / 失败 {fail}")
    return ok


def scan_new(conn, pages=None):
    """扫 topic 查询表里最近更新的仓库，新仓库入库。

    原来只扫 topic:dsh-plugin。实测 topic:deepseek-harness（21,791 个仓库）按更新时间
    取最近 100 个，有 47 个不在目录里 —— 没打 dsh-plugin 这个 topic 的仓库整批漏掉。
    """
    pages = pages if pages is not None else config.GIT_SCAN_PAGES
    added = skipped = 0
    for q_raw in config.GIT_TOPIC_QUERIES:
        q = urllib.parse.quote(q_raw)
        hit = 0
        for page in range(1, pages + 1):
            data, _ = api_get(
                f"{config.GITHUB_API}/search/repositories?q={q}&sort=updated&order=desc&per_page=100&page={page}")
            if not isinstance(data, dict):
                print(f"[scan] {q_raw} 第 {page} 页失败，换下一个查询")
                break
            items = data.get("items", [])
            if not items:
                break
            hit += len(items)
            for it in items:
                full = it.get("full_name", "")
                if not full:
                    continue
                exists = conn.execute(
                    "SELECT 1 FROM plugins WHERE lower(repo)=lower(?)", (full,)).fetchone()
                if exists:
                    continue
                ev, npm, subdir, is_bundle = probe_repo(full)
                if not ev:
                    print(f"[scan] 跳过（非插件）: {full}")
                    continue
                desc = (it.get("description") or "")[:2000]
                has_cjk = bool(CJK_RE.search(desc))
                cat = "bundle" if is_bundle else "plugin"
                tags = ["dsh-plugin"]
                for t in (it.get("topics") or [])[:20]:
                    tags.append(str(t))
                # 分类沿用 seed 的 tags -> category 映射，不再硬编码 tools
                cat_key = config.DEFAULT_CATEGORY
                for t in tags:
                    k = config.TAG_TO_CATEGORY.get(str(t).lower())
                    if k:
                        cat_key = k
                        break
                stars = int(it.get("stargazers_count") or 0)
                pushed = (it.get("pushed_at") or "")[:10]
                created = (it.get("created_at") or "")[:10]
                # 展示名撞车时改用 owner-repo，和 build.py 的撞名前缀规则同一思路。
                # 原来直接用仓库末段，撞名就被 INSERT OR IGNORE 静默丢掉 ——
                # 实测一轮 3 个 topic 查询里有 28 个真实插件这么消失。
                tail = full.split("/")[-1]
                owner = full.split("/")[0]
                disp = tail
                if conn.execute("SELECT 1 FROM plugins WHERE lower(name)=? LIMIT 1",
                                (tail.lower(),)).fetchone():
                    disp = f"{owner}-{tail}"
                cur = conn.execute("""
                    INSERT OR IGNORE INTO plugins
                    (name, repo, path, description, category, official, added, status, evidence,
                     npm, stars, stars_updated, pushed_at, tags, title_en, title_zh,
                     desc_en, desc_zh, install_source, category_key, last_seen)
                    VALUES (?,?,?,?,?,0,?, 'verified', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    disp, full, subdir, desc, cat, created, ev, npm or None,
                    stars, it.get("pushed_at") or "", pushed,
                    json.dumps(tags, ensure_ascii=False),
                    tail.replace("dsh-", "").title(),
                    tail, desc if not has_cjk else "", desc if has_cjk else "",
                    "npm" if npm else "git", cat_key, it.get("pushed_at") or "",
                ))
                # INSERT OR IGNORE 撞 name 时静默不插，此前仍 added+=1 -> 日志虚高
                if cur.rowcount:
                    added += 1
                    print(f"[scan] 新增: {full} (stars={stars}, source={'npm' if npm else 'git'}"
                          f"{', bundle' if is_bundle else ''}{', path=' + subdir if subdir else ''})")
                else:
                    skipped += 1
                    print(f"[scan] 撞名未入库: {full} -> {disp}")
            conn.commit()
            time.sleep(2)  # search API 限速
        print(f"[scan] 查询 {q_raw}：扫到 {hit} 个仓库")
    print(f"[scan] 新增 {added} 个（另有 {skipped} 个因撞名未入库）")
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
    """registry 请求：官方源失败（429/5xx/超时/连接/404）自动降级 npmmirror 镜像，
    每源在 retries+1 轮里各尝试一次；全部失败抛最后异常。
    404 也走镜像：单侧 registry 偶发 404 不该被判定成「包不存在」。"""
    mirror = url.replace(NPM_REGISTRY, NPM_MIRROR, 1) if url.startswith(NPM_REGISTRY) else None
    last = None
    for attempt in range(retries + 1):
        for src in ([url] + ([mirror] if mirror else [])):
            try:
                req = Request(src, headers={"User-Agent": "dsh-store/0.1",
                                            "Accept": "application/json"})
                with urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
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
    if not data or not isinstance(data, dict):
        return {"latest": None, "repo": "", "engines_dsh": None}
    repo_url = ""
    repo_field = data.get("repository")
    if isinstance(repo_field, dict):
        repo_url = repo_field.get("url") or ""
    elif isinstance(repo_field, str):
        repo_url = repo_field
    # engines 可能是字符串/列表等畸形值，直接 .get 会抛 AttributeError，
    # 而它落在 fut.result() 的调用链上，会让整个 incremental 步骤失败、build 与部署都不跑
    engines = data.get("engines")
    eng = engines.get("dsh") if isinstance(engines, dict) else None
    return {"latest": data.get("version"),
            "repo": parse_github_repo(repo_url),
            "engines_dsh": eng}


def updates_npm(conn, batch):
    """npm 更新检测：刷新 npm_meta + npm_repo + dsh_engines；差异满 7 天追平 npm_version。
    按 stars 降序轮转，7 天内已刷过的跳过（全量一轮 7,000+ registry 请求会吃掉整个 CI 时长）。"""
    ensure_schema(conn)
    today = _today()
    cutoff_7 = _days_ago(7)
    rows = conn.execute("""
        SELECT p.name, p.npm FROM plugins p
        LEFT JOIN npm_meta m ON m.pkg = p.npm
        WHERE p.install_source='npm' AND p.npm IS NOT NULL AND p.npm!=''
          AND (m.checked IS NULL OR m.checked <= ?)
        ORDER BY p.stars DESC LIMIT ?
    """, (cutoff_7, batch)).fetchall()
    print(f"[updates-npm] 待刷新 {len(rows)} 个 npm 包（batch={batch}）")
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
        if n_meta % 1000 == 0:
            conn.commit()  # 分批落盘：CI 被取消时不丢整轮结果
    conn.commit()
    print(f"[updates-npm] 完成: npm_meta {n_meta} / 解析仓库 {n_repo} / engines.dsh {n_eng} / 追平 {n_catchup}")
    return n_meta


def updates_git(conn, batch):
    """git 更新检测。两件事拆开做：

    1) 追平（纯 SQL，不花 API 配额）：git_checked 记的是"首次看到差异"的日期，
       满 7 天就把 git_commit 追平到已知的 git_latest_commit。
    2) 探测（花 API）：按 stars 降序轮转刷新默认分支 HEAD，每轮 batch 个，
       7 天内探测过的跳过 —— 节流靠 git_probed，不再复用 git_checked。
       此前两个语义挤在 git_checked 一列上：无差异的仓库会被置空、立刻重新可选，
       于是高星无差异仓库每轮占满 batch，长尾仓库永远轮不到。
    """
    ensure_schema(conn)
    today = _today()
    cutoff_7 = _days_ago(7)

    cur = conn.execute("""
        UPDATE plugins SET git_commit=git_latest_commit, git_checked=''
        WHERE install_source='git' AND git_checked!='' AND git_checked<=?
          AND git_latest_commit IS NOT NULL AND git_latest_commit!=''
          AND git_commit IS NOT NULL AND git_commit!=git_latest_commit
    """, (cutoff_7,))
    n_catchup = cur.rowcount or 0

    rows = conn.execute("""
        SELECT name, repo, git_commit FROM plugins
        WHERE install_source='git' AND repo IS NOT NULL AND repo!=''
          AND (git_probed IS NULL OR git_probed<=?)
        ORDER BY stars DESC LIMIT ?
    """, (cutoff_7, batch)).fetchall()
    print(f"[updates-git] 待探测 {len(rows)} 个仓库（batch={batch}，7 天轮转）")
    if not rows:
        conn.commit()
        print(f"[updates-git] 完成: 追平 {n_catchup} / 新差异 0 / 无差异 0")
        return n_catchup

    def work(item):
        nm, rp, gc = item
        sha = repo_commit(rp)
        return nm, rp, gc, sha

    n_newdiff = n_nodiff = n_failed = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(work, r): r for r in rows}
        done = 0
        for fut in as_completed(futs):
            nm, rp, gc, sha = fut.result()
            done += 1
            # 无论成败都记探测日：失败/404 的仓库下轮起 7 天内不再重复消耗配额
            conn.execute("UPDATE plugins SET git_probed=? WHERE name=?", (today, nm))
            if not sha:
                n_failed += 1
                continue
            if not gc:
                conn.execute("UPDATE plugins SET git_latest_commit=? WHERE name=?", (sha, nm))
            elif sha != gc:
                old_checked = conn.execute(
                    "SELECT git_checked FROM plugins WHERE name=?", (nm,)).fetchone()
                old_checked = (old_checked[0] if old_checked else "") or ""
                if not old_checked:
                    conn.execute(
                        "UPDATE plugins SET git_latest_commit=?, git_checked=? WHERE name=?",
                        (sha, today, nm))
                    n_newdiff += 1
                else:
                    # 差异计时已在跑：仍要把刚观测到的 sha 记下来，
                    # 否则追平通道拿不到新值，要再等一轮 7 天才能追平
                    conn.execute("UPDATE plugins SET git_latest_commit=? WHERE name=?", (sha, nm))
            else:
                # 无差异：记录最新 commit，清空差异计时
                conn.execute(
                    "UPDATE plugins SET git_latest_commit=?, git_checked='' WHERE name=?",
                    (sha, nm))
                n_nodiff += 1
            if done % 200 == 0:
                conn.commit()
                print(f"[updates-git] 进度 {done}/{len(rows)}")
    conn.commit()
    print(f"[updates-git] 完成: 追平 {n_catchup} / 新差异 {n_newdiff} / 无差异 {n_nodiff} / 探测失败 {n_failed}")
    return done


def fill_git_versions(conn, limit):
    """读 git 插件的真实版本号：从**锁定的那个 commit** 上取 package.json#version。
    build 此前对 git 条目恒写占位 0.0.0（参考索引 41/41 都是真版本号），
    宿主按版本判更新于是永远不会提示 git 插件可更新。
    git_ver_commit 记下读自哪个 commit，commit 没变就不重读；读不到也记，避免每轮重复请求。
    走 raw.githubusercontent，不消耗 API 限额。"""
    ensure_schema(conn)
    rows = conn.execute("""
        SELECT name, repo, path, git_commit FROM plugins
        WHERE git_commit IS NOT NULL AND git_commit!=''
          AND repo IS NOT NULL AND repo!=''
          AND (git_version IS NULL OR git_version=''
               OR git_ver_commit IS NULL OR git_ver_commit!=git_commit)
        ORDER BY stars DESC LIMIT ?
    """, (limit,)).fetchall()
    print(f"[gitver] 待读版本 {len(rows)} 个仓库（batch={limit}）")
    if not rows:
        return 0

    def work(item):
        name, repo, path, sha = item
        rel = (path.strip("/") + "/") if path else ""
        data, _ = api_get(f"https://raw.githubusercontent.com/{repo}/{sha}/{rel}package.json")
        ver = data.get("version") if isinstance(data, dict) else None
        return name, sha, (ver if isinstance(ver, str) else "")

    ok = miss = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(work, r): r for r in rows}
        done = 0
        for fut in as_completed(futs):
            name, sha, ver = fut.result()
            done += 1
            if ver:
                conn.execute("UPDATE plugins SET git_version=?, git_ver_commit=? WHERE name=?",
                             (ver, sha, name))
                ok += 1
            else:
                conn.execute("UPDATE plugins SET git_ver_commit=? WHERE name=?", (sha, name))
                miss += 1
            if done % 500 == 0:
                conn.commit()
                print(f"[gitver] 进度 {done}/{len(rows)}，取到 {ok}")
    conn.commit()
    print(f"[gitver] 完成: 取到版本 {ok} / 无 version 字段或读不到 {miss}")
    return ok


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
    ap.add_argument("--gitver", action="store_true", help="从锁定 commit 读 git 插件真实版本号")
    ap.add_argument("--discover", action="store_true",
                    help="四路发现：外部索引地址 + code search + npm 关键词 + 候选判真")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--git-batch", type=int, default=config.GIT_UPDATE_BATCH,
                    help="git 更新检测每轮仓库数（默认 config.GIT_UPDATE_BATCH）")
    ap.add_argument("--gitver-batch", type=int, default=config.GIT_VERSION_BATCH,
                    help="git 版本号读取每轮仓库数（默认 config.GIT_VERSION_BATCH）")
    ap.add_argument("--npm-batch", type=int, default=config.NPM_UPDATE_BATCH,
                    help="npm 更新检测每轮包数（默认 config.NPM_UPDATE_BATCH）")
    ap.add_argument("--icon-batch", type=int, default=config.ICON_BATCH,
                    help="图标探测每轮仓库数（默认 config.ICON_BATCH）")
    args = ap.parse_args()

    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    ensure_schema(conn)
    git_pending = conn.execute(
        "SELECT COUNT(*) FROM plugins WHERE install_source='git' AND (git_commit IS NULL OR git_commit='')"
    ).fetchone()[0]
    ver_pending = conn.execute(
        "SELECT COUNT(*) FROM plugins WHERE git_commit IS NOT NULL AND git_commit!='' "
        "AND (git_version IS NULL OR git_version='' OR git_ver_commit IS NULL OR git_ver_commit!=git_commit)"
    ).fetchone()[0]
    print(f"[incremental] 库内 git 待补 commit: {git_pending} / 待读版本号: {ver_pending}")

    # 发现先行：本轮新收的 git 候选才能马上进入下面的补 commit / 版本号流程
    if args.scan_new or args.discover or args.all:
        scan_new(conn)
    if args.discover or args.all:
        import discover
        pending_before = conn.execute(
            "SELECT COUNT(*) FROM plugins WHERE status='pending'").fetchone()[0]
        stats = discover.run_all(conn, api_get, probe_repo)
        print(f"[discover] 汇总: 候选存量 {pending_before} -> "
              f"判真 {stats.get('verified')} / 判伪 {stats.get('rejected')} / "
              f"索引源新增 {stats.get('extra_index')} / "
              f"code search 新增 {stats.get('code_search')} / "
              f"npm 关键词新增 {stats.get('npm_keyword')}")

    if args.commit_only or args.all:
        limit = config.GIT_COMMIT_BATCH_TOKEN if config.GITHUB_TOKEN else config.GIT_COMMIT_BATCH_NO_TOKEN
        fill_commits(conn, limit)
    if args.downloads or args.all:
        fill_downloads(conn, config.NPM_DL_BATCH)
    if args.updates or args.all:
        updates_npm(conn, args.npm_batch)
        updates_git(conn, args.git_batch)
    if args.icons or args.all:
        icons_probe(conn, args.icon_batch)
    if args.gitver or args.all:
        fill_git_versions(conn, args.gitver_batch)
    conn.close()


if __name__ == "__main__":
    main()
