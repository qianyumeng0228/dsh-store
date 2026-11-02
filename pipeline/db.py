# -*- coding: utf-8 -*-
"""db.py —— 共享的 schema 迁移与小工具。纯标准库，幂等可重跑。

- ensure_schema(conn): 老库幂等追加新列与新表（npm_meta / icon_probe）。
- parse_github_repo(url): 从 npm repository.url 各种形态解析 owner/name。
- load_icon_maps(): 读取 dshopen / awesome 图标索引，返回归一化映射，供 build 组装 icon。
"""
import json
import re
import sqlite3
from pathlib import Path

import config

# plugins 表新增列（老库 ALTER 追加，新库 CREATE 时也应包含）
PLUGIN_NEW_COLS = {
    "npm_repo": "TEXT",          # npm 包解析出的 GitHub 仓库 owner/name（lowercase）
    "dsh_engines": "TEXT",       # npm engines.dsh 原始字符串
    "git_latest_commit": "TEXT",  # git 源仓库最新 commit sha
    "git_checked": "TEXT",        # 差异开始日（YYYY-MM-DD），无已知差异时为空
}


def ensure_schema(conn):
    """幂等：缺表建表，缺列补列。

    plugins 表还不存在时（空库首跑）只建自己的两张附表并返回，
    主表由调用方 seed.py 建好后再补列。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS npm_meta (
            pkg TEXT PRIMARY KEY, latest TEXT, latest_since TEXT, checked TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS icon_probe (
            repo TEXT PRIMARY KEY, icon_path TEXT, checked TEXT
        )
    """)
    has_plugins = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plugins'").fetchone()
    if not has_plugins:
        conn.commit()
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(plugins)")]
    for col, typ in PLUGIN_NEW_COLS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE plugins ADD COLUMN {col} {typ}")
            cols.append(col)
    conn.commit()


_REPO_GH = re.compile(r"github\.com[:/]([^/\s]+)/([^\s#?]+?)(?:\.git)?(?=[/#?\s]|$)")


def parse_github_repo(url):
    """从各种 repository.url 形态解析 'owner/name'（小写），失败返回 ''。
    支持: https://github.com/o/r(.git)(/tree/..), git+https://..., git://...,
          git@github.com:o/r.git, github:o/r。"""
    if not url:
        return ""
    s = str(url).strip()
    s = re.sub(r"^git\+", "", s)
    s = re.sub(r"^(https?://|git://)", "", s)
    s = re.sub(r"^git@github\.com:", "github.com/", s)
    m = _REPO_GH.search(s)
    if m:
        owner, name = m.group(1), m.group(2)
    else:
        m2 = re.match(r"github:([^/\s]+)/([^/\s#?\.]+)", s)
        if not m2:
            return ""
        owner, name = m2.group(1), m2.group(2)
    if name.endswith(".git"):
        name = name[:-4]
    if not owner or not name or "/" in name:
        return ""
    return f"{owner.lower()}/{name.lower()}"


def _norm_name(s):
    """归一化插件名用于匹配：小写、去掉 #子目录后缀、去掉 npm scope 前缀。"""
    s = (s or "").strip().lower()
    s = s.split("#", 1)[0]
    if s.startswith("@") and "/" in s:
        return s.split("/", 1)[1]
    return s


def _norm_repo(url):
    repo = parse_github_repo(url or "")
    return repo


def load_icon_maps():
    """读取图标次级源，返回 (dshopen, awesome)。
    每项为 {'name': {norm_name: icon_url}, 'repo': {'owner/name': icon_url}}。
    源文件缺失/损坏时对应项返回空映射，不抛异常。"""
    dshopen = {"name": {}, "repo": {}}
    awesome = {"name": {}, "repo": {}}

    def feed(target, name, repo_url, icon_url):
        if not icon_url:
            return
        k = _norm_name(name)
        if k:
            target["name"].setdefault(k, icon_url)
        r = _norm_repo(repo_url)
        if r:
            target["repo"].setdefault(r, icon_url)

    # ① dshopen 索引（本地副本；缺失时尝试下载一次，失败则跳过）
    try:
        if not config.DSHOPEN_FILE.exists():
            import urllib.request
            req = urllib.request.Request(config.DSHOPEN_INDEX_URL,
                                        headers={"User-Agent": "dsh-store/0.1"})
            with urllib.request.urlopen(req, timeout=90) as r:
                config.DSHOPEN_FILE.write_bytes(r.read())
        data = json.loads(config.DSHOPEN_FILE.read_text(encoding="utf-8"))
        for p in data.get("plugins", []):
            icon = p.get("icon") or ""
            if icon:
                icon = config.DSHOPEN_BASE.rstrip("/") + "/" + icon.lstrip("/")
            repo_url = ""
            if isinstance(p.get("repository"), dict):
                repo_url = p["repository"].get("url") or ""
            feed(dshopen, p.get("name"), repo_url, icon)
    except Exception as e:
        print(f"[icon] dshopen 索引读取失败，跳过该源: {e}")

    # ② awesome-dsh-plugin 索引（本地副本；缺失时尝试下载一次，失败则跳过）
    try:
        if not config.AWESOME_FILE.exists():
            import urllib.request
            req = urllib.request.Request(config.AWESOME_URL,
                                        headers={"User-Agent": "dsh-store/0.1"})
            with urllib.request.urlopen(req, timeout=60) as r:
                config.AWESOME_FILE.write_bytes(r.read())
        data = json.loads(config.AWESOME_FILE.read_text(encoding="utf-8"))
        for p in data.get("plugins", []):
            repo_url = p.get("url") or ""
            if not repo_url and p.get("owner"):
                repo_url = f"https://github.com/{p['owner']}/"
            feed(awesome, p.get("name"), repo_url, p.get("icon") or "")
    except Exception as e:
        print(f"[icon] awesome 索引读取/下载失败，跳过该源: {e}")

    return dshopen, awesome
