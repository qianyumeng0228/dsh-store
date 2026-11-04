# -*- coding: utf-8 -*-
"""discover.py —— 四路"漏网之鱼"发现通道。

实测出来的缺口（2026-11-04）：
- 全生态的公开索引地址里有 4 个没直接接（官方 dshmarket 在用），各能带来 220~740 条净新增；
- GitHub 侧只扫 topic:dsh-plugin，而 topic:deepseek-harness 最近 100 个仓库有 47 个不在目录；
- npm 侧此前**从不查 npm search API**，npm 条目只能靠种子或 GitHub 反查带进来，
  抽样复核后约 8% 带官方关键词的 npm 包因此进不来。

共同口径：外部索引/搜索结果一律只落 status='pending' 候选，
必须经 verify_pending 用 package.json 里的 dsh 签名判真后才升 verified ——
和 scan_new 的判定方式一致，不轻信第三方索引，也不让它们的脏数据直接进站点。
"""
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from db import parse_github_repo
from seed import split_desc, make_title

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
UA = {"User-Agent": "dsh-store/0.1", "Accept": "application/json"}
PLUGINISH_KEYWORDS = {"dsh", "dsh-plugin", "deepseek-harness", "deepseek harness", "cordis"}


def _http_json(url, timeout=40, retries=2, headers=None):
    """GET + 解析 JSON；失败重试，最终失败返回 None（调用方按缺源处理）。"""
    hdr = dict(UA)
    if headers:
        hdr.update(headers)
    for attempt in range(retries + 1):
        try:
            with urlopen(Request(url, headers=hdr), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (404, 451):
                return None
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
        except Exception:
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    return None


def _cat_key(tags):
    for t in tags:
        k = config.TAG_TO_CATEGORY.get(str(t).lower())
        if k:
            return k
    return config.DEFAULT_CATEGORY


def _known(conn, name="", repo="", npm=""):
    """同一个插件在三个身份维度上都可能已经存在：展示名、仓库、npm 包名。"""
    row = conn.execute(
        """SELECT 1 FROM plugins
           WHERE lower(coalesce(name,''))=? OR lower(coalesce(repo,''))=?
              OR lower(coalesce(npm,''))=?
           LIMIT 1""",
        ((name or "\x00").lower(), (repo or "\x00").lower(), (npm or "\x00").lower())).fetchone()
    return row is not None


def _free_name(conn, repo, npm=None):
    """展示名撞车时改用 owner-repo，和 build.py 的撞名前缀规则同一思路，
    否则真实存在的插件会被 INSERT OR IGNORE 静默丢掉。"""
    base = npm or (repo.split("/")[-1] if repo else "")
    if not base:
        return ""
    if not conn.execute("SELECT 1 FROM plugins WHERE lower(name)=? LIMIT 1",
                        (base.lower(),)).fetchone():
        return base
    if repo and "/" in repo:
        qualified = f"{repo.split('/')[0]}-{base}"
        if not conn.execute("SELECT 1 FROM plugins WHERE lower(name)=? LIMIT 1",
                            (qualified.lower(),)).fetchone():
            return qualified
    return ""


def add_candidate(conn, *, repo="", npm="", path="", desc="", tags=(), stars=0,
                  pushed="", created="", source="", evidence=""):
    """落一条 pending 候选。返回 True 表示真的新增了。"""
    name = _free_name(conn, repo, npm)
    if not name:
        return False
    if _known(conn, name=name, repo=repo, npm=npm):
        return False
    en, zh = split_desc(desc or "")
    tags = [str(t) for t in tags][:12]
    # 命名占位符：这条 INSERT 有 23 列，位置参数数错一次的代价是静默写错列
    conn.execute(
        """INSERT OR IGNORE INTO plugins
           (name, repo, path, description, category, official, added, last_verified,
            verified_against, status, evidence, npm, stars, stars_updated, pushed_at,
            tags, title_en, title_zh, desc_en, desc_zh, install_source, category_key, last_seen)
           VALUES (:name, :repo, :path, :desc, 'plugin', 0, :created, '', '',
            'pending', :evidence, :npm, :stars, :pushed, :pushed,
            :tags, :title, :disp, :en, :zh, :source, :cat_key, :pushed)""",
        {"name": name, "repo": repo, "path": path, "desc": (desc or "")[:2000],
         "created": created, "evidence": evidence or f"discover:{source}",
         "npm": npm or None, "stars": int(stars or 0), "pushed": pushed,
         "tags": json.dumps(tags, ensure_ascii=False),
         "title": make_title(repo, name), "disp": name, "en": en, "zh": zh,
         "source": "npm" if npm and not repo else "git", "cat_key": _cat_key(tags)})
    conn.commit()
    return conn.execute("SELECT 1 FROM plugins WHERE lower(name)=? AND status='pending' LIMIT 1",
                        (name.lower(),)).fetchone() is not None


# ---------- 通道一：外部索引地址 ----------

def _text(v):
    """format-1 系索引里 description/title 可能是字符串，也可能是 {en, zh} 字典。
    中文优先（本站就是中文优先）。"""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("zh", "zh-CN", "cn", "en"):
            got = v.get(k)
            if isinstance(got, str) and got.strip():
                return got
        for got in v.values():
            if isinstance(got, str) and got.strip():
                return got
    return ""


def _entries_format1(data):
    for e in (data.get("plugins") or []):
        if not isinstance(e, dict):
            continue
        repo = parse_github_repo(e.get("repository") if isinstance(e.get("repository"), str)
                                 else (e.get("repository") or {}).get("url"))
        name = (e.get("name") or "").strip()
        meta = e.get("metadata") if isinstance(e.get("metadata"), dict) else {}
        yield {
            "repo": repo,
            "npm": "" if repo else name,
            "path": meta.get("directory") or "",
            "desc": _text(e.get("description")),
            "tags": meta.get("keywords") or [],
            "stars": 0,
            "pushed": _text(meta.get("updated")),
            "created": _text(meta.get("created")),
        }


def _entries_topic_snapshot(data):
    """YELEBAI 那份是 GitHub topic 快照：字段是 owner/repo/fullName/topics。"""
    for e in (data.get("plugins") or []):
        if not isinstance(e, dict):
            continue
        full = (e.get("fullName") or "").strip()
        if not full and e.get("owner") and e.get("repo"):
            full = f"{e['owner']}/{e['repo']}"
        yield {
            "repo": full.lower(),
            "npm": "",
            "path": "",
            "desc": _text(e.get("description")),
            "tags": e.get("topics") or [],
            "stars": int(e.get("stars") or 0),
            "pushed": (e.get("updatedAt") or "")[:10],
            "created": (e.get("createdAt") or "")[:10],
        }


PARSERS = {"format1": _entries_format1, "topic_snapshot": _entries_topic_snapshot}


def ingest_extra_indexes(conn):
    """把外部索引地址读成候选。只认它们"说有"，真伪交给 verify_pending。"""
    total_new = 0
    for tag, url, kind in config.EXTRA_INDEX_SOURCES:
        parser = PARSERS.get(kind)
        data = _http_json(url, timeout=120)
        if not isinstance(data, dict) or parser is None:
            print(f"[discover] 索引源跳过 {tag}: 拉不到或格式未知 ({kind})")
            continue
        new = seen = 0
        for item in parser(data):
            seen += 1
            if seen > config.EXTRA_INDEX_MAX:
                break
            if add_candidate(conn, source=tag, **item):
                new += 1
        conn.commit()
        total_new += new
        print(f"[discover] 索引源 {tag}: 读到 {seen} 条，新增候选 {new}")
    return total_new


# ---------- 通道二：GitHub code search（兜没打 topic 的仓库） ----------

def code_search_candidates(conn, api_get):
    """code search 需要用户态 token；Actions 的 GITHUB_TOKEN 常被拒（403），
    所以这条路失败就安静退出，不影响其它通道。"""
    added = 0
    for q in config.CODE_SEARCH_QUERIES:
        seen_repos = set()
        hit = 0
        for page in range(1, config.CODE_SEARCH_PAGES + 1):
            url = (f"{config.GITHUB_API}/search/code?q={urllib.parse.quote(q)}"
                   f"&per_page={min(config.CODE_SEARCH_MAX, 100)}&page={page}")
            data, status = api_get(url, timeout=25)
            if not isinstance(data, dict) or not (data.get("items") or []):
                if page == 1:
                    print(f"[discover] code search 不可用（{status or '无结果'}），跳过：{q}")
                break
            hit += len(data["items"])
            for it in data["items"]:
                # 只认命中文件就是 package.json 的：code search 也会命中 lock 文件、
                # 文档、vendored 依赖里的同名字符串（实测这类占了一半以上）
                path = (it.get("path") or "").replace("\\", "/")
                if not path.endswith("package.json") or "node_modules/" in path:
                    continue
                repo = ((it.get("repository") or {}).get("full_name") or "").strip()
                if not repo or repo.lower() in seen_repos:
                    continue
                seen_repos.add(repo.lower())
                if _known(conn, repo=repo):        # 先查库，省掉一次 /repos 请求
                    continue
                info, _ = api_get(repo_api(repo), timeout=20)
                if not isinstance(info, dict):
                    continue
                if add_candidate(conn, repo=repo.lower(),
                                 desc=_text(info.get("description")),
                                 tags=info.get("topics") or [],
                                 stars=int(info.get("stargazers_count") or 0),
                                 pushed=(info.get("pushed_at") or "")[:10],
                                 created=(info.get("created_at") or "")[:10],
                                 source="code-search",
                                 evidence=f"codesearch:{q}"):
                    added += 1
            conn.commit()
            time.sleep(6)  # code search 限速 10 req/min
        print(f"[discover] code search [{q}] 命中 {hit} 个文件、{len(seen_repos)} 个仓库，新增候选 {added}")
    return added


def repo_api(repo):
    return f"{config.GITHUB_API}/repos/{urllib.parse.quote(repo)}"


# ---------- 通道三：npm 关键词 ----------

def _npm_pluginish(manifest):
    """判定 npm 包是不是 DSH 插件：package.json 的 dsh 字段 / engines.dsh /
    依赖 @deepseek-ai/dsh* / 官方关键词。名字前缀不算判据。"""
    if not isinstance(manifest, dict):
        return "", "", False
    dsh = manifest.get("dsh")
    deps = manifest.get("dependencies") or manifest.get("peerDependencies") or {}
    host_dep = isinstance(deps, dict) and any(
        k in deps for k in ("@deepseek-ai/dsh", "@deepseek-ai/dsh-tools", "@deepseek-ai/dsh-agent"))
    engines = (manifest.get("engines") or {}).get("dsh") if isinstance(manifest.get("engines"), dict) else None
    kws = {str(k).lower() for k in (manifest.get("keywords") or [])}
    is_bundle = isinstance(dsh, dict) and "bundle" in dsh
    if dsh is not None or engines is not None or host_dep:
        ev = "npm#dsh" + ("bundle" if is_bundle else "")
        return ev, manifest.get("name") or "", is_bundle
    if kws & PLUGINISH_KEYWORDS and ("dsh" in kws or "dsh-plugin" in kws or "deepseek-harness" in kws):
        return "npm#keywords", manifest.get("name") or "", False
    return "", "", False


def scan_npm_keywords(conn, pages=None, max_new=None):
    """npm search 拿候选名单，逐个取 packument 判真。
    判真的直接落 verified（npm 源不需要 probe_repo 那次 raw 请求）。"""
    pages = pages if pages is not None else config.NPM_DISCOVER_PAGES
    max_new = max_new if max_new is not None else config.NPM_DISCOVER_MAX_NEW
    fetched = verified = skipped = 0
    for kw in config.NPM_KEYWORDS:
        for page in range(pages):
            url = (f"{config.NPM_SEARCH_API}?text={urllib.parse.quote(kw)}"
                   f"&size=250&from={page * 250}")
            data = _http_json(url, timeout=40)
            objs = (data or {}).get("objects") or []
            if not objs:
                break
            for m in objs:
                pkg = m.get("package") or {}
                name = (pkg.get("name") or "").strip()
                if not name or _known(conn, name=name, npm=name):
                    skipped += 1
                    continue
                if fetched >= max_new:
                    print(f"[discover] npm 校验已达单轮上限 {max_new}，剩余下轮继续")
                    return verified
                doc = _http_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@')}",
                                 timeout=25, retries=1)
                fetched += 1
                if not isinstance(doc, dict):
                    continue
                latest = (doc.get("dist-tags") or {}).get("latest") or ""
                manifest = (doc.get("versions") or {}).get(latest) or {}
                ev, pkg_name, is_bundle = _npm_pluginish(manifest)
                if not ev:
                    continue
                repo = parse_github_repo(manifest.get("repository")
                                         if isinstance(manifest.get("repository"), str)
                                         else (manifest.get("repository") or {}).get("url"))
                times = doc.get("time") or {}
                tags = [str(k) for k in (manifest.get("keywords") or [])][:12]
                en, zh = split_desc(manifest.get("description") or "")
                disp = _free_name(conn, repo, pkg_name or name)
                if not disp or _known(conn, name=disp, repo=repo, npm=pkg_name or name):
                    skipped += 1
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO plugins
                       (name, repo, path, description, category, official, added,
                        last_verified, verified_against, status, evidence, npm, stars,
                        stars_updated, pushed_at, tags, title_en, title_zh, desc_en,
                        desc_zh, install_source, npm_version, category_key, last_seen)
                       VALUES (:name, :repo, '', :desc, :cat, 0, :created, '', '',
                        'verified', :ev, :npm, 0, :modified, :modified, :tags, :title,
                        :disp, :en, :zh, 'npm', :latest, :cat_key, :modified)""",
                    {"name": disp, "repo": repo, "desc": (manifest.get("description") or "")[:2000],
                     "cat": "bundle" if is_bundle else "plugin",
                     "created": (times.get("created") or "")[:10], "ev": ev,
                     "npm": pkg_name or name, "modified": (times.get("modified") or "")[:10],
                     "tags": json.dumps(tags, ensure_ascii=False),
                     "title": make_title(repo, name), "disp": name, "en": en, "zh": zh,
                     "latest": latest, "cat_key": _cat_key(tags)})
                verified += 1
            conn.commit()
            time.sleep(0.4)
        print(f"[discover] npm 关键词 [{kw}] 完成：校验 {fetched} 个包，新增 {verified}，跳过 {skipped}")
    return verified


# ---------- 收口：候选判真 ----------

def verify_pending(conn, probe_repo, limit=None):
    """pending → verified / rejected。probe_repo 读仓库里的 package.json 找 dsh 签名。"""
    from incremental import _today, _days_ago
    limit = limit or config.PENDING_VERIFY_BATCH
    rows = conn.execute(
        """SELECT name, repo, npm FROM plugins WHERE status='pending'
           ORDER BY stars DESC LIMIT ?""", (limit,)).fetchall()
    print(f"[discover] 待判真候选 {len(rows)} 个（本轮上限 {limit}）")
    ok = bad = 0
    for name, repo, npm in rows:
        if repo:
            ev, npm_pkg, subdir, is_bundle = probe_repo(repo)
            if not ev:
                conn.execute("UPDATE plugins SET status='rejected', last_seen=? WHERE name=?",
                             (_today(), name))
                bad += 1
            else:
                tags = conn.execute("SELECT tags, category_key FROM plugins WHERE name=?",
                                    (name,)).fetchone()
                conn.execute(
                    """UPDATE plugins SET status='verified', evidence=?, npm=?, path=?,
                       category=?, install_source=?, last_verified=?, verified_against=?,
                       git_checked='', last_seen=? WHERE name=?""",
                    (ev, npm_pkg or npm, subdir or "", "bundle" if is_bundle else "plugin",
                     "npm" if npm_pkg else "git", _today(), "", _today(), name))
                ok += 1
        elif npm:
            doc = _http_json(f"https://registry.npmjs.org/{urllib.parse.quote(npm, safe='@')}",
                             timeout=25, retries=1)
            latest = ((doc or {}).get("dist-tags") or {}).get("latest") or ""
            manifest = ((doc or {}).get("versions") or {}).get(latest) or {}
            ev, _, is_bundle = _npm_pluginish(manifest)
            if not ev:
                conn.execute("UPDATE plugins SET status='rejected', last_seen=? WHERE name=?",
                             (_today(), name))
                bad += 1
            else:
                conn.execute(
                    """UPDATE plugins SET status='verified', evidence=?, install_source='npm',
                       npm_version=?, category=?, last_verified=?, verified_against=?,
                       last_seen=? WHERE name=?""",
                    (ev, latest, "bundle" if is_bundle else "plugin", _today(), "",
                     _today(), name))
                ok += 1
        else:
            conn.execute("UPDATE plugins SET status='rejected', last_seen=? WHERE name=?",
                         (_today(), name))
            bad += 1
        conn.commit()
        time.sleep(0.15)
    print(f"[discover] 判真完成：verified {ok} / rejected {bad}")
    return ok, bad


def run_all(conn, api_get, probe_repo, npm_pages=None, npm_max_new=None):
    """四路发现里需要独立跑的两路（topic 扫描仍在 incremental.scan_new 里）。
    返回计数 dict，供 incremental 打总账。"""
    stats = {}
    stats["extra_index"] = ingest_extra_indexes(conn)
    stats["code_search"] = code_search_candidates(conn, api_get)
    stats["npm_keyword"] = scan_npm_keywords(conn, pages=npm_pages, max_new=npm_max_new)
    ok, bad = verify_pending(conn, probe_repo)
    stats["verified"] = ok
    stats["rejected"] = bad
    return stats
