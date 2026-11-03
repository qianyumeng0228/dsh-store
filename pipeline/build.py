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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from db import ensure_schema, load_icon_maps
import translate

NPM_REGISTRY = "https://registry.npmjs.org"
NPM_MIRROR = "https://registry.npmmirror.com"


def http_get_json(url, timeout=20, retries=2):
    """官方源失败（429/5xx/超时/连接/404）自动降级 npmmirror 镜像。
    404 也要试镜像：单侧 registry 偶发 404 时不该直接判定包不存在。"""
    mirror = url.replace(NPM_REGISTRY, NPM_MIRROR, 1) if url.startswith(NPM_REGISTRY) else None
    last = None
    for i in range(retries + 1):
        for src in ([url] + ([mirror] if mirror else [])):
            try:
                req = Request(src, headers={"User-Agent": "dsh-store/0.1", "Accept": "application/json"})
                with urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
                continue
        if i < retries:
            time.sleep(1.5 * (i + 1))
    raise last


def npm_latest(name: str):
    """返回 (version, engines_dsh) 或 None。"""
    try:
        data = http_get_json(f"{NPM_REGISTRY}/{name}/latest")
        if not isinstance(data, dict):
            return None
        ver = data.get("version")
        if not ver:
            return None
        engines = data.get("engines")
        # engines 可能是字符串/列表等畸形值；这里若抛异常会连版本号一起丢掉
        eng = engines.get("dsh") if isinstance(engines, dict) else None
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
               official, evidence, status, added, verified_against,
               npm_repo, dsh_engines, git_latest_commit, git_version
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


def load_npm_meta(conn):
    """npm 包 latest 版本映射（incremental.py --updates 维护）。"""
    m = {}
    try:
        for r in conn.execute("SELECT pkg, latest FROM npm_meta"):
            m[r[0]] = r[1]
    except sqlite3.OperationalError:
        pass
    return m


def load_npm_meta_checked(conn):
    """npm 包最近一次刷新日期，用于给「已确认 404」设时限。"""
    m = {}
    try:
        for r in conn.execute("SELECT pkg, checked FROM npm_meta"):
            m[r[0]] = r[1] or ""
    except sqlite3.OperationalError:
        pass
    return m


def load_icon_probe(conn):
    """repo -> 命中的图标相对路径映射（incremental.py --icons 维护）。"""
    m = {}
    try:
        for r in conn.execute("SELECT repo, icon_path FROM icon_probe WHERE icon_path IS NOT NULL AND icon_path!=''"):
            m[r[0].lower()] = r[1]
    except sqlite3.OperationalError:
        pass
    return m


def row_pname(r):
    """索引内的唯一展示名。npm 包名全局唯一，只要声明了包名就用它 —— 不看 npm_version
    是否已补齐，否则「补上版本」那一天条目名会从仓库末段翻成包名，身份不稳定。"""
    if r["install_source"] == "npm" and r["npm"]:
        return r["npm"]
    repo = r["repo"] or ""
    return repo.split("/")[-1] if repo else (r["name"] or "")


def main():
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    ensure_schema(conn)
    rows = fetch_rows(conn, config.INDEX_STATUSES, config.INDEX_CATEGORIES)
    print(f"[build] 候选（verified+plugin/bundle）: {len(rows)}")

    # 1) npm 插件补收录版本：优先复用 incremental 已抓到的 npm_meta，只对没记录的包打 registry
    npm_meta_map = load_npm_meta(conn)
    npm_rows = [r for r in rows if r["install_source"] == "npm"]
    need = [r for r in npm_rows if not r["npm_version"]]
    from_meta = [r for r in need if npm_meta_map.get(r["npm"])]
    for r in from_meta:
        conn.execute("UPDATE plugins SET npm_version=? WHERE name=?",
                     (npm_meta_map[r["npm"]], r["name"]))
    conn.commit()
    # npm_meta 里 latest 为空的包按 404 处理，但只跳过 30 天：
    # registry 偶发 404 / 包重新上架时不该让条目永久失去版本号
    npm_checked = load_npm_meta_checked(conn)
    stale_404 = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    retry = [r for r in need if npm_checked.get(r["npm"], "") <= stale_404]
    print(f"[build] npm 插件 {len(npm_rows)} 个，待补版本 {len(need)} 个："
          f"npm_meta 直接补齐 {len(from_meta)} / 需请求 registry {len(retry)} / "
          f"404 未满 30 天跳过 {len(need) - len(from_meta) - len(retry)}")
    if retry:
        done = 0
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(npm_latest, r["npm"]): r for r in retry}
            for fut in as_completed(futs):
                r = futs[fut]
                res = fut.result()
                done += 1
                if res:
                    ver, eng = res
                    conn.execute(
                        "UPDATE plugins SET npm_version=?,"
                        " dsh_engines=COALESCE(NULLIF(dsh_engines,''), ?) WHERE name=?",
                        (ver, eng or "", r["name"]))
                    conn.commit()
                if done % 500 == 0:
                    print(f"[build] npm 版本进度 {done}/{len(retry)}")
        conn.commit()

    # 2) 组装索引
    plugins_out = []
    site_plugins = []
    seen_names = set()
    cat_stats = {}
    skipped_git_nocommit = 0
    dup = 0
    qualified = 0
    tr_candidates = []   # (英文原文, site 条目引用, index 条目引用)，组装完统一机翻
    downloads_map = load_downloads(conn)
    probe_map = load_icon_probe(conn)
    dshopen_maps, awesome_maps = load_icon_maps()
    print(f"[build] 图标源: dshopen {len(dshopen_maps['name'])} 条 / awesome {len(awesome_maps['name'])} 条 / 探测命中 {len(probe_map)} 个仓库")

    rows = fetch_rows(conn, config.INDEX_STATUSES, config.INDEX_CATEGORIES)
    # 展示名撞名：同一展示名下出现多个不同仓库时，用仓库名命名的条目加 owner- 前缀。
    # 否则按 stars 降序只会留下一个，其余真实插件被当成重复整条丢弃。
    name_repos = {}
    for r in rows:
        key = row_pname(r).lower()
        repo = (r["repo"] or "").lower()
        if key and repo:
            name_repos.setdefault(key, set()).add(repo)
    conflict_bases = {k for k, v in name_repos.items() if len(v) > 1}
    print(f"[build] 撞名展示名 {len(conflict_bases)} 个（跨多仓库，git 条目加 owner- 前缀）")
    for r in rows:
        name = r["name"]; repo = r["repo"]; path = r["path"]
        title_en = r["title_en"]; title_zh = r["title_zh"]
        desc_en = r["desc_en"]; desc_zh = r["desc_zh"]
        tags = r["tags"]; stars = r["stars"]
        install_source = r["install_source"]; npm = r["npm"]
        git_commit = r["git_commit"]; npm_version = r["npm_version"]
        cat_key = r["category_key"]; pushed_at = r["pushed_at"]
        official = r["official"]
        npm_repo = r["npm_repo"] or ""
        dsh_engines = r["dsh_engines"] or ""
        git_latest = r["git_latest_commit"] or ""
        try:
            tags = json.loads(tags) if tags else []
        except Exception:
            tags = []

        # ---- 新字段：author / avatar / latestVersion / hasUpdate / dsh / icon ----
        if install_source == "npm":
            author = npm_repo.split("/")[0] if "/" in npm_repo else ""
            latest = npm_meta_map.get(npm) if npm else None
            latest_version = latest or ""
            has_update = bool(latest) and bool(npm_version) and latest != npm_version
            dsh_str = dsh_engines or ""
        else:
            author = repo.split("/")[0] if "/" in repo else ""
            latest_version = git_latest[:7] if git_latest else ""
            has_update = bool(git_latest) and bool(git_commit) and git_latest != git_commit
            dsh_str = ""
        avatar = f"https://github.com/{author}.png?size=64" if author else ""

        def _icon_from(maps):
            k = (name or "").lower()
            rk = (repo or "").lower()
            if k and k in maps["name"]:
                return maps["name"][k]
            if rk and rk in maps["repo"]:
                return maps["repo"][rk]
            return ""

        icon = _icon_from(dshopen_maps) or _icon_from(awesome_maps)
        if not icon and repo and repo.lower() in probe_map:
            icon = f"https://raw.githubusercontent.com/{repo}/HEAD/{probe_map[repo.lower()]}"

        # ---- 决定可安装性与安装标识 ----
        installable = False
        src = None
        pname = row_pname(r)
        name_from_repo = not (install_source == "npm" and npm)
        if install_source == "npm" and npm_version:
            installable = True
            src = {"type": "npm", "registry": NPM_REGISTRY}
        elif git_commit:
            # git 源，或 npm 版本还没补上：先按 commit 装，补齐版本后升级为 npm
            installable = True
            src = {"type": "git", "url": f"https://github.com/{repo}", "commit": git_commit}
            # monorepo 子目录：参考索引把 path 同时放进 source 与 repository.directory，
            # 只写 directory 的话宿主会装到仓库根
            if path:
                src["path"] = path
        else:
            skipped_git_nocommit += 1

        if name_from_repo and pname.lower() in conflict_bases:
            owner = repo.split("/")[0].lower() if "/" in repo else ""
            if owner:
                pname = f"{owner}-{pname}"
                qualified += 1

        if pname in seen_names:
            dup += 1
            continue
        seen_names.add(pname)

        # ---- format-1 条目 ----
        ver_obj = {}
        if installable:
            # git 条目用锁定 commit 上读到的真实版本号（参考索引里 git 条目都是真版本，
            # 恒写 0.0.0 会让宿主永远判不出可更新）；读不到才回落占位值
            if src["type"] == "npm":
                version = npm_version
            else:
                version = r["git_version"] or "0.0.0"
            ts = iso_to_ts(pushed_at) or iso_to_ts(
                conn.execute("SELECT last_verified FROM plugins WHERE name=?", (r["name"],)).fetchone()[0])
            ver_obj = {
                "version": version,
                "source": src,
                # 参考索引 979/979 都带 engines（无要求时为 {}）；不写就等于把
                # 宿主机侧的版本门槛整个丢掉，只剩网站自己在前端过滤
                "engines": ({"dsh": dsh_str} if dsh_str else {}),
            }
            if ts:
                ver_obj["publishTimestamp"] = ts
        entry = {
            "name": pname,
            "title": {"en": title_en or pname, "zh": title_zh or title_en or pname},
            "description": {"en": desc_en or "", "zh": desc_zh or ""},
            "author": author,
            "avatar": avatar,
            "icon": icon,
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
            "version": npm_version or (r["git_version"] if install_source != "npm" else "") or "",
            "verified": r["verified_against"] or "",
            "downloads": downloads_map.get(npm, 0) if install_source == "npm" else 0,
            "author": author, "avatar": avatar, "icon": icon,
            "dsh": dsh_str, "latestVersion": latest_version, "hasUpdate": bool(has_update),
        })
        cat_stats[cat_key] = cat_stats.get(cat_key, 0) + 1
        # 最终展示的描述里没有中文、又有英文原文可翻的，才进机翻队列
        if desc_en and not translate.has_cjk(desc_zh or desc_en or ""):
            tr_candidates.append((desc_en, site_plugins[-1], entry))

    # ---- 描述机翻（中文优先）----
    # 六成条目只有英文描述，中文用户浏览吃力。这里把没有中文的描述批量机翻进
    # site 的 desc 与索引的 description.zh，原文留在 descEn / description.en。
    # 结果按源文本哈希进 store.db，每日只翻新增。接口是非官方的，任何异常都只
    # 降级成"保留英文"，绝不拖垮整轮构建。
    try:
        translate.ensure_table(conn)
        pairs = translate.translate_many(
            conn, [t[0] for t in tr_candidates], cap=config.TRANSLATE_MAX)
        done = 0
        for src, site_entry, index_entry in tr_candidates:
            zh = pairs.get(src)
            if not zh:
                continue
            site_entry["desc"] = zh
            index_entry["description"]["zh"] = zh
            done += 1
        print(f"[build] 描述中文化：{done}/{len(tr_candidates)} 条待翻条目已有中文")
    except Exception as e:
        print(f"[build] 描述机翻整步跳过（保留英文原文）：{type(e).__name__} {e}")

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
    print(f"[build] index.json: {len(plugins_out)} 条可索引（含不可装占位 {skipped_git_nocommit}，"
          f"撞名限定 {qualified}，重复合并 {dup}）")

    # ---- 可直接粘进宿主 Discover 的索引源 ----
    # 全量索引里过半条目没有 versions（浏览目录要它们，装了没有意义）。宿主原生
    # 「发现」页把每条都当成可装项呈现，缺 versions 就是一行点了没反应的死胡同，
    # 所以另出一份只保留能装的安装索引，体积也小一半以上。
    installable_out = [p for p in plugins_out if p.get("versions")]
    install_index = dict(index)
    install_index["plugins"] = installable_out
    install_index["title"] = {
        "en": "DSH Plugin Store (installable)",
        "zh": "DSH 插件商店（仅可安装）",
    }
    install_index["description"] = {
        "en": "Installable-only slice of the DSH community plugin index: every entry "
              "carries a resolvable version and a source the CLI can install. Add this "
              "file as an index source under Plugins → Discover.",
        "zh": "DSH 社区插件索引的可安装切片：每条都带可解析的版本与可安装的来源。"
              "把本文件加成 Plugins → Discover 的索引源即可浏览安装。",
    }
    (config.DIST_DIR / "index-installable.json").write_text(
        json.dumps(install_index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[build] index-installable.json: {len(installable_out)} 条（已剔除无 versions {len(plugins_out) - len(installable_out)} 条）")

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
