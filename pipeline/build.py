# -*- coding: utf-8 -*-
"""build.py —— SQLite 主库 → dist/index.json（format-1 索引）+ 网站数据。
- npm 插件：调 npm registry 补 latest 版本号（有缓存，仅首次跑全量）
- git 插件：只收录已补齐 commit 的（incremental.py 负责补齐）
"""
import json
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

NPM_REGISTRY = "https://registry.npmjs.org"


def http_get_json(url, timeout=20, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "dsh-store/0.1", "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            if i < retries:
                time.sleep(1.5 * (i + 1))
    raise last


def npm_latest(name: str):
    """返回 (version, engines_dsh) 或 None。"""
    try:
        data = http_get_json(f"{NPM_REGISTRY}/{name}/latest")
        ver = data.get("version")
        if not ver:
            return None
        eng = (data.get("engines") or {}).get("dsh")
        return ver, eng
    except Exception:
        return None


def iso_to_ts(s: str):
    """ISO 日期字符串 -> unix 秒；失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return None


def fetch_rows(conn, statuses, categories):
    q = """
        SELECT name, repo, path, title_en, title_zh, desc_en, desc_zh, tags, stars,
               install_source, npm, git_commit, npm_version, category_key, pushed_at,
               official, evidence, status, added, verified_against
        FROM plugins
        WHERE status IN ({})
          AND category IN ({})
        ORDER BY stars DESC
    """.format(",".join("?" * len(statuses)), ",".join("?" * len(categories)))
    conn.row_factory = sqlite3.Row
    return conn.execute(q, list(statuses) + list(categories)).fetchall()


def load_downloads(conn):
    """npm 包近 30 天下载量映射（incremental.py 每日补齐）。"""
    m = {}
    try:
        for r in conn.execute("SELECT pkg, downloads FROM npm_downloads"):
            m[r[0]] = r[1]
    except sqlite3.OperationalError:
        pass
    return m


def main():
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    rows = fetch_rows(conn, config.INDEX_STATUSES, config.INDEX_CATEGORIES)
    print(f"[build] 候选（verified+plugin/bundle）: {len(rows)}")

    # 1) npm 插件补版本（有缓存的跳过）
    npm_rows = [r for r in rows if r["install_source"] == "npm"]
    need = [r for r in npm_rows if not r["npm_version"]]
    print(f"[build] npm 插件 {len(npm_rows)} 个，待补版本 {len(need)} 个")
    if need:
        done = 0
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = {ex.submit(npm_latest, r["npm"]): r for r in need}
            for fut in as_completed(futs):
                r = futs[fut]
                res = fut.result()
                done += 1
                if res:
                    ver, eng = res
                    conn.execute("UPDATE plugins SET npm_version=? WHERE name=?",
                                 (ver, r["name"]))
                    conn.commit()
                if done % 500 == 0:
                    print(f"[build] npm 版本进度 {done}/{len(need)}")
        conn.commit()

    # 2) 组装索引
    plugins_out = []
    site_plugins = []
    seen_names = set()
    cat_stats = {}
    skipped_git_nocommit = 0
    dup = 0
    downloads_map = load_downloads(conn)

    rows = fetch_rows(conn, config.INDEX_STATUSES, config.INDEX_CATEGORIES)
    for r in rows:
        name = r["name"]; repo = r["repo"]; path = r["path"]
        title_en = r["title_en"]; title_zh = r["title_zh"]
        desc_en = r["desc_en"]; desc_zh = r["desc_zh"]
        tags = r["tags"]; stars = r["stars"]
        install_source = r["install_source"]; npm = r["npm"]
        git_commit = r["git_commit"]; npm_version = r["npm_version"]
        cat_key = r["category_key"]; pushed_at = r["pushed_at"]
        official = r["official"]
        try:
            tags = json.loads(tags) if tags else []
        except Exception:
            tags = []

        # ---- 决定可安装性与安装标识 ----
        installable = False
        src = None
        if install_source == "npm" and npm_version:
            installable = True
            pname = npm
            src = {"type": "npm"}
        elif install_source == "git" and git_commit:
            installable = True
            pname = repo.split("/")[-1]
            src = {"type": "git", "url": f"https://github.com/{repo}", "commit": git_commit}
        elif install_source == "npm":
            # 版本还没补上：先降级为 git，待 incremental 补 commit 后升级
            if git_commit:
                installable = True
                pname = repo.split("/")[-1]
                src = {"type": "git", "url": f"https://github.com/{repo}", "commit": git_commit}
            else:
                skipped_git_nocommit += 1
                installable = False
                pname = repo.split("/")[-1]
        else:
            skipped_git_nocommit += 1
            installable = False
            pname = repo.split("/")[-1]

        if pname in seen_names:
            dup += 1
            continue
        seen_names.add(pname)

        # ---- format-1 条目 ----
        ver_obj = {}
        if installable:
            version = npm_version if install_source == "npm" else "0.0.0"
            ts = iso_to_ts(pushed_at) or iso_to_ts(
                conn.execute("SELECT last_verified FROM plugins WHERE name=?", (r["name"],)).fetchone()[0])
            ver_obj = {
                "version": version,
                "source": src,
            }
            if ts:
                ver_obj["publishTimestamp"] = ts
        entry = {
            "name": pname,
            "title": {"en": title_en or pname, "zh": title_zh or title_en or pname},
            "description": {"en": desc_en or "", "zh": desc_zh or ""},
            "author": repo.split("/")[0] if "/" in repo else "",
            "homepage": f"https://github.com/{repo}#readme" if repo else "",
            "repository": {"url": f"https://github.com/{repo}"} if repo else {},
            "metadata": {
                "category": cat_key,
                "keywords": tags,
                "labels": [],
                "sortKeys": {"github-stars": stars},
            },
        }
        if path:
            entry["repository"]["directory"] = path
        if installable:
            entry["versions"] = [ver_obj]
        plugins_out.append(entry)

        # ---- 网站数据 ----
        install_cmd = f"dsh plugin add {pname}" if install_source == "npm" else f"dsh plugin add github:{repo}"
        added = r["added"] if "added" in r.keys() else ""
        site_plugins.append({
            "name": pname, "title": title_zh or title_en or pname,
            "titleEn": title_en or pname, "desc": desc_zh or desc_en or "",
            "descEn": desc_en or "", "cat": cat_key,
            "stars": stars, "tags": tags[:8], "repo": repo,
            "install": install_cmd if installable else None,
            "installable": installable,
            "source": install_source,
            "updated": pushed_at or "",
            "added": added,
            "official": bool(official),
            "version": npm_version or "",
            "verified": r["verified_against"] or "",
            "downloads": downloads_map.get(npm, 0) if install_source == "npm" else 0,
        })
        cat_stats[cat_key] = cat_stats.get(cat_key, 0) + 1

    # ---- format-1 索引 ----
    categories_meta = {}
    for k in cat_stats:
        d = config.CATEGORY_DEFS.get(k, {"en": k, "zh": k})
        categories_meta[k] = {"title": {"en": d["en"], "zh": d["zh"]}}
    index = {
        "format": 1,
        "title": {"en": "DSH Plugin Store", "zh": "DSH 插件商店"},
        "description": {
            "en": "Community plugin index for DeepSeek Harness. Add this file as an index source under Plugins → Discover.",
            "zh": "DeepSeek Harness 社区插件索引。把本文件加成 Plugins → Discover 的索引源即可浏览安装。",
        },
        "metadata": {
            "categories": categories_meta,
            "labels": {},
            "sortKeys": {},
        },
        "plugins": plugins_out,
    }
    (config.DIST_DIR / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[build] index.json: {len(plugins_out)} 条可索引（含不可装占位 {skipped_git_nocommit}）")

    # ---- 网站数据 ----
    site_data = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "total": len(site_plugins),
        "installable": sum(1 for p in site_plugins if p["installable"]),
        "categories": {k: {"en": config.CATEGORY_DEFS.get(k, {"en": k})["en"],
                           "zh": config.CATEGORY_DEFS.get(k, {"zh": k})["zh"],
                           "count": cat_stats.get(k, 0)} for k in cat_stats},
        "plugins": site_plugins,
    }
    (config.SITE_DIR / "data").mkdir(parents=True, exist_ok=True)
    (config.SITE_DIR / "data" / "plugins.json").write_text(
        json.dumps(site_data, ensure_ascii=False), encoding="utf-8")
    # 网站源码（仓库 site/ 目录）合并进输出
    src_html = config.ROOT / "site" / "index.html"
    if src_html.exists():
        shutil.copy(src_html, config.SITE_DIR / "index.html")
    print(f"[build] 网站数据: {len(site_plugins)} 条，可一键装 {site_data['installable']} 条")
    print(f"[build] 分类分布: {json.dumps(cat_stats, ensure_ascii=False)}")
    conn.close()


if __name__ == "__main__":
    main()
