# -*- coding: utf-8 -*-
"""seed.py —— 导入 dsh.works 大库种子 → SQLite 主库。
只做搬运+初步清洗：字段规范化、标题/描述中英拆分、标签分类映射。
判定真伪（evidence）与安装方式（npm/git）在 build 阶段使用。
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
STAR_PREFIX_RE = re.compile(r"^\s*★\s*\d+\s*[│|]\s*")


def ensure_seed():
    """种子文件不存在时自动下载（云端 Actions 无本地缓存）。"""
    if config.SEED_FILE.exists():
        return
    config.ensure_dirs()
    print(f"[seed] 下载种子: {config.SEED_URL}")
    req = Request(config.SEED_URL, headers={"User-Agent": "dsh-store/0.1"})
    for attempt in range(3):
        try:
            with urlopen(req, timeout=300) as r:
                data = r.read()
            config.SEED_FILE.write_bytes(data)
            print(f"[seed] 下载完成: {len(data)} bytes")
            return
        except Exception as e:
            print(f"[seed] 下载失败(第{attempt+1}次): {e}")
            time.sleep(5)
    raise RuntimeError("种子下载失败")


def split_desc(desc: str):
    """把描述拆成 {en, zh}。描述常为 '★ 102 │ 英文 │ 中文' 或单语言。"""
    desc = (desc or "").strip()
    desc = STAR_PREFIX_RE.sub("", desc).strip()
    if not desc:
        return "", ""
    has_cjk = bool(CJK_RE.search(desc))
    if not has_cjk:
        return desc, ""
    # 含中文：尝试按 │ | 拆
    parts = [p.strip() for p in re.split(r"[│|]", desc) if p.strip()]
    en, zh = "", ""
    for p in parts:
        if CJK_RE.search(p):
            zh = p if not zh else zh
        else:
            en = p if not en else en
    if not zh:
        zh = desc
    return en, zh


def make_title(repo: str, name: str):
    """从 repo 名生成标题。owner/repo -> Repo 名，去掉 dsh- 前缀。"""
    base = repo.split("/")[-1] if repo else name
    base = re.sub(r"^dsh[-_]", "", base, flags=re.I)
    base = re.sub(r"^@?", "", base)
    words = re.split(r"[-_]", base)
    t = " ".join(w.capitalize() for w in words if w)
    return t or name


def main():
    config.ensure_dirs()
    ensure_seed()
    src = config.SEED_FILE
    if not src.exists():
        print(f"[seed] 种子文件不存在: {src}")
        sys.exit(1)
    data = json.loads(src.read_text(encoding="utf-8"))
    plugins = data.get("plugins", [])
    print(f"[seed] 读取 {len(plugins)} 条 (updated={data.get('updated')})")

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    from db import ensure_schema
    ensure_schema(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS plugins (
            name            TEXT PRIMARY KEY,
            repo            TEXT,
            path            TEXT,
            description     TEXT,
            category        TEXT,
            official        INTEGER DEFAULT 0,
            added           TEXT,
            last_verified   TEXT,
            verified_against TEXT,
            status          TEXT,
            evidence        TEXT,
            npm             TEXT,
            stars           INTEGER DEFAULT 0,
            stars_updated   TEXT,
            pushed_at       TEXT,
            tags            TEXT,
            title_en        TEXT,
            title_zh        TEXT,
            desc_en         TEXT,
            desc_zh         TEXT,
            install_source  TEXT,          -- npm | git
            git_commit      TEXT,
            npm_version     TEXT,
            category_key    TEXT,
            last_seen       TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON plugins(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cat ON plugins(category_key)")

    COLS = ("name", "repo", "path", "description", "category", "official", "added",
            "last_verified", "verified_against", "status", "evidence", "npm", "stars",
            "stars_updated", "pushed_at", "tags", "title_en", "title_zh", "desc_en",
            "desc_zh", "install_source", "git_commit", "npm_version", "category_key",
            "last_seen")
    placeholders = ",".join("?" * len(COLS))
    # UPDATE 时跳过增量维护字段：git_commit / npm_version 由 incremental 和 build 维护，seed 不得覆盖
    SKIP_UPD = ("name", "git_commit", "npm_version")
    upd_cols = ",".join(f"{c}=?" for c in COLS if c not in SKIP_UPD)

    n_new = n_upd = 0
    with conn:
        for p in plugins:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            repo = (p.get("repo") or "").strip()
            desc = p.get("description") or ""
            en, zh = split_desc(desc)
            title_en = p.get("title_en") or make_title(repo, name)
            title_zh = p.get("title_zh") or title_en
            tags = p.get("tags") or []
            # 分类映射：第一个命中的 tag
            cat_key = config.DEFAULT_CATEGORY
            for t in tags:
                k = config.TAG_TO_CATEGORY.get(str(t).lower())
                if k:
                    cat_key = k
                    break
            npm = (p.get("npm") or "").strip()
            row = {
                "name": name, "repo": repo, "path": p.get("path") or "",
                "description": desc, "category": p.get("category") or "plugin",
                "official": 1 if p.get("official") else 0, "added": p.get("added") or "",
                "last_verified": p.get("lastVerified") or "",
                "verified_against": p.get("verifiedAgainst") or "",
                "status": p.get("status") or "unverified",
                "evidence": p.get("evidence") or "", "npm": npm or None,
                "stars": int(p.get("stars") or 0), "stars_updated": p.get("starsUpdated") or "",
                "pushed_at": p.get("pushedAt") or "",
                "tags": json.dumps(tags, ensure_ascii=False),
                "title_en": title_en, "title_zh": title_zh, "desc_en": en, "desc_zh": zh,
                "install_source": "npm" if npm else "git", "git_commit": None,
                "npm_version": None, "category_key": cat_key,
                "last_seen": data.get("updated") or "",
            }
            vals = [row[c] for c in COLS]
            cur = conn.execute("SELECT 1 FROM plugins WHERE name=?", (name,))
            if cur.fetchone():
                upd_vals = [row[c] for c in COLS if c not in SKIP_UPD] + [name]
                conn.execute(f"UPDATE plugins SET {upd_cols} WHERE name=?", upd_vals)
                n_upd += 1
            else:
                conn.execute(f"INSERT INTO plugins ({','.join(COLS)}) VALUES ({placeholders})", vals)
                n_new += 1

    conn.close()
    total = sqlite3.connect(config.DB_PATH).execute("SELECT COUNT(*) FROM plugins").fetchone()[0]
    print(f"[seed] 完成: 新增 {n_new} / 更新 {n_upd} / 库内总计 {total}")


if __name__ == "__main__":
    main()
