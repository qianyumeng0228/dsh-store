[English](README.md) | 中文

[![build](https://github.com/qianyumeng0228/dsh-store/actions/workflows/build-deploy.yml/badge.svg)](https://github.com/qianyumeng0228/dsh-store/actions/workflows/build-deploy.yml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![catalog](https://img.shields.io/badge/catalog-29,593_plugins-blue)](https://qianyumeng0228.github.io/dsh-store/)

# dsh-store

DSH 插件商店背后的**目录本身**：一条纯 Python 管道，负责发现、核验并发布
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的社区插件清单，
外加一个静态浏览站和两份机器可读的索引源。

| | |
|---|---|
| 网站 | https://qianyumeng0228.github.io/dsh-store/ |
| 索引源（推荐） | `https://qianyumeng0228.github.io/dsh-store/index-installable.json` |
| 全量索引（含暂不可装条目） | `https://qianyumeng0228.github.io/dsh-store/index.json` |
| 网站数据（页面加载的那份） | `https://qianyumeng0228.github.io/dsh-store/data/plugins.json` |
| 宿主端商店插件 | [qianyumeng0228/dsh-plug-store](https://github.com/qianyumeng0228/dsh-plug-store) |

全程无需人工：每次 push 与每天 03:17 UTC 的定时任务都会重跑
种子 → 发现 → 核验 → 构建 → Pages 部署。

## 当前数据

取自 2026-11-04 线上 `data/plugins.json` 实测：

| | |
|---|---|
| 收录插件 | 29,593 |
| 可一键安装 | 14,344（npm 来源 10,598，其余为 git） |
| 分类 | 17 个，中英双语 |
| 中文描述占比 | 约 99%（构建期机翻，原文保留在 `descEn`） |
| 对生态内其他公开索引的覆盖 | 每份 92–96%，总量为其超集 |

最后一行就是这个仓库存在的意义：它把生态里各自独立的注册表**合并**，而不是照抄其中一份。

## 一条记录怎么才算被收录

只有当管道在**仓库自身**里找到证据时才会收录：`package.json` 带 `dsh` 字段、
带 `dsh.bundle` 声明，或依赖 `@deepseek-ai/dsh*`。名字和 topic 只是线索，从不是判据——
所以不带 `dsh-` 前缀的插件照样收，乱打标记的收不进来。

发现通道全部自动化：

| 通道 | 贡献 |
|---|---|
| `dsh.works` 种子 | 最大的单份注册表，每日刷新 |
| `awesome-dsh-plugin.com` | 人工整理目录 + 图标映射 |
| `dshopen.org` | 人工整理目录 + 图标映射 |
| GitHub topic 扫描 | `topic:dsh-plugin`、`topic:deepseek-harness`、`topic:dsh` |
| npm 关键词爬取 | `keywords:dsh-plugin`、`keywords:deepseek-harness` + manifest 核验 |
| 外部索引镜像 | 其他商店发布的 `index.json`，只作为**候选** |
| GitHub code search | 兜底：带 manifest 签名但没打 topic 的仓库 |

外部索引一律不直接采信：它们的条目只落成 `status='pending'` 候选，必须经 `package.json`
核验通过才升级为收录。这就是本目录能宣称覆盖它们 92–96%、却不必继承其中死链的原因。

## 在 DSH 里使用

**作为索引源**：DSH → Plugins → Discover → Index sources，粘贴

```
https://qianyumeng0228.github.io/dsh-store/index-installable.json
```

请用这一份而不是 `index.json`：全量索引保留了"已收录但还装不了"的条目（网站统计与分类需要它们），
而宿主的「发现」会把每一行都渲染成可安装项——贴全量那份，你会得到几千行点了没反应的死条目。
可安装那份也更小：原始约 8.7MB，gzip 后约 1.8MB，落在宿主 15 秒的加载窗口内。

**命令行安装**：

```sh
dsh plugin add <name>                 # npm 来源
dsh plugin add github:<owner/repo>    # git 来源
```

## 仓库结构

```
dsh-store/
├── config.py              # 路径、收录规则、分类映射、每轮批量上限
├── pipeline/
│   ├── db.py              # 幂等 schema 迁移、repository URL 解析、图标源映射
│   ├── seed.py            # 种子导入：dsh.works JSON → SQLite（清洗、中英拆分、分类）
│   ├── discover.py        # 发现通道：外部索引、code search、npm 关键词
│   ├── incremental.py     # 补 commit、读版本号、更新检测、图标探测、topic 扫描、发现
│   ├── translate.py       # 纯英文描述的批量机翻
│   └── build.py           # SQLite → dist/index.json（format-1）、index-installable.json、网站数据
├── site/index.html        # 商店 UI —— 唯一真源，副本同步进插件仓库
├── index.json             # 供 jsDelivr 镜像的最新全量索引（由 CI 提交）
├── data/                  # 原始种子与 store.db（gitignore；DB 靠 Actions 缓存跨运行累积）
├── dist/                  # 构建产物（gitignore）
└── .github/workflows/     # build-deploy.yml：全自动流水线
```

这里的 `site/index.html` 是**唯一**应当修改的副本；插件仓库保留一份字节一致的拷贝，
宿主专用差异由同一份文件在运行时检测，不做两份各自维护。

## 本地跑一遍

```sh
python pipeline/seed.py                 # 刷新种子
python pipeline/incremental.py --all    # 发现 + 补 commit + 版本号 + 更新检测 + 图标
python pipeline/build.py                # 产出索引与网站数据
```

`GITHUB_TOKEN` 很关键：没有它 API 配额只有 60 次/小时，每个阶段都会退化成小批量。
`store.db` 是增量成果的唯一载体——云端用 `actions/cache` 持久化，缓存键**必须每轮唯一**并配
`restore-keys`：同一个键的缓存不可覆盖，用固定键会让数据库冻结在首日快照，每天补好的成果都被丢掉
（这个坑踩过，表现为收录数长期不涨）。

## 提交插件

用网站的「提交插件」页，它会自动生成一个预填好的本仓库 issue；或者直接开：
https://github.com/qianyumeng0228/dsh-store/issues/new

收录条件：仓库可达，且 `package.json` 带 DSH 证据，并且要么已发布 npm 包、要么有宿主能安装的
git 来源。已收录条目每天复核；失效、盗用或名不副实的会被移除。给仓库加上 `topic:dsh-plugin`
就不需要提交，管道会自己扫到。

## 已知边界

- **git 长尾。** git 来源的条目要先解析到 commit 才算可安装。目前仍有约 1.7 万条在排队，
  每轮约补 2,000 条（受 API 配额与构建时长限制），所以可装覆盖是逐日爬升而不是一次到位。
- **「有新版本」是相对目录而言。** 本站看不到你本机装了哪些版本，只跟上次收录的版本比。
  按机器判断该不该更新，是宿主插件的职责。
- **机翻就是机翻。** 中文描述由英文自动生成，原文一并保留（`desc` / `descEn`），不会丢。
- **定时构建不保证准点。** 03:17 UTC 的 schedule 实测过延后数小时起跑、甚至整日未触发。
  需要确定性刷新请改用 `workflow_dispatch`。
- **`index.json` 入库**是为了 jsDelivr 镜像，每次实质变更让仓库体积增加约 11MB；
  如果哪天受不了，应改成 Release 资产或孤儿分支存放。
- 收录不等于背书：每一条都是第三方代码。

## 许可

MIT
