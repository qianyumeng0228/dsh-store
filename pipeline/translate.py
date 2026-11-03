"""英文描述机翻中文。

设计约束（都是实测出来的，改动前请先读完）：

* 接口用 Google 网页翻译 `translate_a/single?client=gtx`，无需 key，返回
  `Access-Control-Allow-Origin: *`。它**不是官方 API**，随时可能限流或改协议，
  所以这里所有失败都必须降级成"保留原文"，绝不能让构建挂掉。
* 批量：把多条描述用 `SEP` 拼成一次请求。实测纯换行拼接会被 Google 合并成
  一段（拆不回来），带 `@@@` 这种行内标记的分隔符才能可靠拆回；拆回条数不符
  就整批降级成逐条请求，逐条再失败就跳过。
* 缓存进主库 `store.db`（Actions 里跨运行保留），按源文本哈希命中就不翻，
  所以每日只翻新增/变更的描述。缓存被清也只是重跑一轮，不影响正确性。
"""

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
SEP = "\n@@@\n"          # 行内标记分隔符：Google 会把它当独立段保留，能可靠拆回
MARK = "@@@"
CJK = re.compile(r"[\u4e00-\u9fff]")

BATCH = 20              # 每请求条数：太小请求数爆炸，太大容易被整段合并
TIMEOUT = 25
RETRIES = 3
SLEEP = 0.35            # 请求间隔，免费接口别打爆


def has_cjk(text):
    return bool(CJK.search(text or ""))


def cache_key(src):
    return hashlib.sha1(("en|zh-CN|" + src).encode("utf-8")).hexdigest()


def ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS translations (
            key TEXT PRIMARY KEY, src TEXT NOT NULL, zh TEXT NOT NULL, created TEXT NOT NULL
        )
    """)
    conn.commit()


def load_cached(conn, texts):
    """按源文本取已译结果。IN 参数分片，SQLite 默认变量上限别踩雷。"""
    out = {}
    uniq = list(dict.fromkeys(t for t in texts if t and not has_cjk(t)))
    for i in range(0, len(uniq), 400):
        chunk = uniq[i:i + 400]
        rows = conn.execute(
            "SELECT src, zh FROM translations WHERE key IN (%s)"
            % ",".join("?" * len(chunk)),
            [cache_key(t) for t in chunk],
        ).fetchall()
        for src, zh in rows:
            out[src] = zh
    return out


def store(conn, pairs):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for src, zh in pairs:
        conn.execute(
            "INSERT OR REPLACE INTO translations (key, src, zh, created) VALUES (?,?,?,?)",
            (cache_key(src), src, zh, now),
        )
    conn.commit()


def _request(q):
    url = ENDPOINT + "?" + urllib.parse.urlencode({
        "client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": q,
    })
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    segs = [s[0] for s in (data[0] or []) if s and isinstance(s, list) and s[0]]
    return "".join(segs)


def _split(joined, expect):
    """按标记拆回 expect 条；条数不符或空段都算拆回失败。"""
    parts = [p.strip() for p in joined.split(MARK)]
    if len(parts) != expect:
        return None
    if not all(parts):
        return None
    return parts


def translate_batch(texts):
    """一次请求翻多条，返回 list[str]；失败抛异常由调用方降级。"""
    last = None
    for attempt in range(RETRIES):
        try:
            joined = _request(SEP.join(texts))
            parts = _split(joined, len(texts))
            if parts is not None:
                return parts
            last = "拆回条数不符（%d != %d）" % (0 if not parts else len(parts), len(texts))
            break            # 合并问题重试同一批没用，直接交给逐条降级
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (429, 500, 502, 503):
                time.sleep(1.5 * (attempt + 1) ** 2)
                continue
            raise
        except Exception as e:  # URLError / 超时 / JSON 解析
            last = str(e)
            time.sleep(1.5 * (attempt + 1) ** 2)
    raise RuntimeError(last or "翻译失败")


def translate_many(conn, texts, cap=None, log=print):
    """返回 {原文: 中文}。只翻没中文的、且库里没有的；失败静默跳过。"""
    todo = list(dict.fromkeys(t for t in texts if t and not has_cjk(t)))
    cached = load_cached(conn, todo)
    out = dict(cached)
    missing = [t for t in todo if t not in cached]
    if cap is not None:
        missing = missing[:cap]
    if not missing:
        log(f"[translate] 全部命中缓存（{len(cached)} 条），无需请求")
        return out

    log(f"[translate] 待翻 {len(missing)} 条（缓存命中 {len(cached)}），批量 {BATCH} 条/请求")
    ok = 0
    fail = 0
    for i in range(0, len(missing), BATCH):
        batch = missing[i:i + BATCH]
        try:
            pairs = list(zip(batch, translate_batch(batch)))
        except Exception as e:
            # 整批失败 → 逐条兜一次。原文与译文必须成对记下来：过滤掉失败条目后再按顺序
            # 和 batch 配对会串位，把 A 的译文存成 B 的。
            saved = []
            for t in batch:
                try:
                    one = _split(_request(t), 1)
                    if one:
                        saved.append((t, one[0]))
                except Exception:
                    pass
                time.sleep(SLEEP)
            log(f"[translate] 批次 {i // BATCH + 1} 批量失败（{e}），逐条救回 {len(saved)}/{len(batch)}")
            if not saved:
                fail += len(batch)
                continue
            pairs = saved
        store(conn, pairs)
        out.update(dict(pairs))
        ok += len(pairs)
        time.sleep(SLEEP)
    log(f"[translate] 本轮新增 {ok} 条，失败 {fail} 条（失败保留英文原文）")
    return out
