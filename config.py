# -*- coding: utf-8 -*-
"""DSH 插件商店 —— 全局配置。
纯 Python 标准库实现，零第三方依赖，方便在服务器上直接跑。
"""
import os
from pathlib import Path

# ---------- 路径 ----------
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "store.db"
DIST_DIR = ROOT / "dist"
SITE_DIR = DIST_DIR / "site"

# ---------- 种子数据源 ----------
SEED_FILE = RAW_DIR / "dshworks-plugins.json"   # dsh.works 大库（21,928 条）
SEED_URL = "https://dsh.works/awesome-dsh-plugins/plugins.json"

# ---------- 收录规则 ----------
# 索引只收这些状态与类型的条目（网站可放宽展示）
INDEX_STATUSES = ("verified",)
INDEX_CATEGORIES = ("plugin", "bundle")

# ---------- GitHub API ----------
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"
GITHUB_UA = {"User-Agent": "dsh-store/0.1", "Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    GITHUB_UA["Authorization"] = f"Bearer {GITHUB_TOKEN}"

# 无 token 时，单轮最多给多少个 git-only 插件补 commit（60 req/h 限额下的保守值）
GIT_COMMIT_BATCH_NO_TOKEN = 40
# 有 token 时单轮上限
GIT_COMMIT_BATCH_TOKEN = 2000

# npm 下载量（近30天）单轮补齐上限（GitHub Actions runner 对 npm API 限流较严，用小批量防超时）
NPM_DL_BATCH = 800

# 描述机翻单轮上限。首轮把存量英文描述一次翻完（约 16k 条 / 20 条一批 ≈ 800 次请求，
# 十几分钟）；之后每天只有新增条目要翻，这个值就当保险丝用。设 0 可整步关闭机翻。
TRANSLATE_MAX = int(os.environ.get("TRANSLATE_MAX", "20000"))

# git 更新检测：每轮刷新 git_latest_commit 的仓库数（按 stars 降序轮转，7 天内已探测的跳过）。
# 规模要和 7 天窗口配套：库内约 1.4 万个 git 仓库，500/轮 × 7 天 = 3,500 只能覆盖头部，
# 长尾依旧饿死；2,000/轮 × 7 天 ≈ 全量转一圈。
GIT_UPDATE_BATCH = 2000
# git 插件真实版本号：从锁定的 commit 读 package.json#version（raw 请求，不吃 API 限额）。
# 只在 commit 变化时重读，首轮需要把存量摊掉，故单独设批量。
GIT_VERSION_BATCH = 2500
# npm 更新检测：每轮刷新的包数（按 stars 降序轮转，7 天内已刷过的跳过）。
# 全量一轮 7,000+ registry 请求，是 CI 耗时主因，分批后每天只刷到期的一轮。
NPM_UPDATE_BATCH = 2000
# 图标探测：每轮 HEAD 探测的仓库数（按 stars 降序，7 天内已探测的跳过）
ICON_BATCH = 400

# 图标次级源（dshopen.org / awesome-dsh-plugin.com）
DSHOPEN_BASE = "https://dshopen.org/"
DSHOPEN_INDEX_URL = DSHOPEN_BASE + "index.json"
DSHOPEN_FILE = RAW_DIR / "dshopen.json"
AWESOME_URL = "https://awesome-dsh-plugin.com/plugins.json"
AWESOME_FILE = RAW_DIR / "awesome-plugins.json"

# ---------- 分类（tags -> categories，中英双语） ----------
# 参考 awesome-dsh-plugin.com 的 16 分类体系 + dsh.works 的 tags
CATEGORY_DEFS = {
    "tools":          {"en": "Tools & Capabilities", "zh": "工具与能力"},
    "memory":         {"en": "Memory",               "zh": "记忆"},
    "agents":         {"en": "Agents & Workflow",    "zh": "Agent 与工作流"},
    "interface":      {"en": "Interface & UI",       "zh": "界面增强"},
    "models":         {"en": "Models & Accounts",    "zh": "模型与账号接入"},
    "interop":        {"en": "System Interop",       "zh": "系统互操作"},
    "usage":          {"en": "Usage & Billing",      "zh": "用量与计费"},
    "knowledge":      {"en": "Knowledge",            "zh": "知识库"},
    "safety":         {"en": "Safety & Privacy",     "zh": "安全与隐私"},
    "terminal":       {"en": "Terminal",             "zh": "终端"},
    "marketplace":    {"en": "Marketplace",          "zh": "插件市场"},
    "vision":         {"en": "Vision & Multimodal",  "zh": "视觉与多模态"},
    "observability":  {"en": "Observability",        "zh": "可观测与调试"},
    "fun":            {"en": "Fun & Customization",  "zh": "趣味与定制"},
    "devtools":       {"en": "Dev Tools",            "zh": "开发工具"},
    "notifications":  {"en": "Notifications",        "zh": "通知"},
    "channels":       {"en": "Channels & Comms",     "zh": "渠道与通信"},
    "documents":      {"en": "Documents & Rendering","zh": "文档与渲染"},
    "browser":        {"en": "Browser & Web",        "zh": "浏览器与网页"},
    "voice":          {"en": "Voice & Audio",        "zh": "语音与音频"},
}

TAG_TO_CATEGORY = {
    "capabilities": "tools",
    "tools": "tools",
    "memory": "memory",
    "agents": "agents",
    "agent": "agents",
    "ui": "interface",
    "theme": "interface",
    "themes": "interface",
    "models": "models",
    "interop": "interop",
    "usage": "usage",
    "knowledge": "knowledge",
    "safety": "safety",
    "terminal": "terminal",
    "marketplace": "marketplace",
    "vision": "vision",
    "observability": "observability",
    "devtools": "devtools",
    "notifications": "notifications",
    "channels": "channels",
    "document": "documents",
    "documents": "documents",
    "rendering": "documents",
    "browser": "browser",
    "web": "browser",
    "voice": "voice",
    "audio": "voice",
    "speech": "voice",
    "fun": "fun",
}

# 无 tag 命中时的兜底分类
DEFAULT_CATEGORY = "tools"


def ensure_dirs():
    for d in (DATA_DIR, RAW_DIR, DIST_DIR, SITE_DIR):
        d.mkdir(parents=True, exist_ok=True)
