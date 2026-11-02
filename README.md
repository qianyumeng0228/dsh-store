# DSH 插件商店

面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的社区插件商店：**format-1 索引源 + 静态浏览站**。在 DSH 的 Plugins → Discover → Index sources 里加一行索引地址，即可浏览、搜索、一键安装；网站供日常逛和查。

**线上地址（GitHub Pages 自动部署）**：
- 网站：https://qianyumeng0228.github.io/dsh-store/
- 索引源：https://qianyumeng0228.github.io/dsh-store/index.json
- 仓库：https://github.com/qianyumeng0228/dsh-store

每次 push / 每天 03:17 UTC 由 GitHub Actions 自动执行：种子刷新 → git commit 补齐 → npm 版本/更新检测 → 图标探测 → topic 扫描 → 生成索引 → 发布 Pages，无需手动运维。

## 当前数据（2026-11-02 13:58 UTC 实测线上 `data/plugins.json`）

| 指标 | 数值 |
|---|---|
| 索引收录（去重后输出） | 25,987 |
| 其中可一键安装 | 11,742（来源：npm 8,591 / git 17,396） |
| 种子来源 | dsh.works `plugins.json`（21,928 条，每日刷新） |
| 作者头像覆盖（真实 GitHub 头像） | 19,231 |
| 插件图标（dshopen + awesome 双源 + GitHub 路径探测） | 1,124 |
| 有新版本标记 | 203 |
| DSH 适配版本区间（engines.dsh） | 1,763 |
| npm 近 30 天下载量已补齐 | 389 |
| 判定标准 | dsh.works 逐条核验的 `package.json#dsh` 字段证据 + 自建 topic 扫描复核 |
| 分类 | 17 个双语分类（工具与能力 / 记忆 / 界面增强 / 模型与账号接入 …） |

> 本表此前长期停在 17,447 / 可装 4,830，低于 README 早先宣称的 17,799 / 6,637。原因不是插件变少，而是每日增量成果没有累积：
> `actions/cache` 用了固定 key（同 key 缓存不可覆盖，命中后 post 步跳过上传），`store.db` 被冻结在首日快照，
> 每轮补好的 git commit / npm 版本 / 下载量 / 图标探测 / 新仓库扫描随运行结束一起丢弃。
> 另有一处 `build.py` 的 npm 镜像降级因 `HTTPError` 未导入而整段失效，导致收录版本号补不上、可装数被压低。
> 两处已修复，上表就是修复后连续累积的结果（索引 +8.5k、可装 +6.9k、图标 626→1,124、engines.dsh 1,081→1,763）。

## 功能（对标 dsh-market 的可移植部分）

- **作者头像**：卡片/行式视图展示插件作者真实 GitHub 头像（`github.com/<owner>.png`），无头像不占位。
- **插件图标**：有图标才显示（dshopen 索引 → awesome 目录 → GitHub 仓库常见路径探测），没有不占位。
- **🔄 有新版本**：数据管道每日比对 npm registry 最新版 / GitHub 默认分支最新提交，标记"有新版本 vX.Y.Z / 有新提交"；支持按此筛选、已安装插件标"可更新"。
- **DSH 适配过滤**：按 `engines.dsh` 区间过滤"兼容当前 DSH 版本"（版本号自填，未声明宿主要求的插件不隐藏，与 dsh-market 口径一致）。
- **提交插件**：网站内置「提交插件」标签页——填表单一键生成 GitHub Issue 预填链接，附收录规则。
- 其余：发现/主题/收藏/已安装/已屏蔽/高级 6 页、17 分类计数、搜索、多维筛选（可安装/官方/7天新增/🔄有新版本/收藏/已安装）、7 种排序（含随机）、卡片/行式双视图、🕘最近收录 + 🔥最多安装 横滑分区、派生标签（🆕新增/◷更新/↓下载量分档）、详情弹层（作者/版本/下载量/DSH 适配/安装命令）、底部待装栏、本地收藏/已装/屏蔽、备份导出导入、索引源诊断。

## 项目结构

```
dsh-store/
├── config.py              # 路径 / 收录规则 / 分类映射 / GitHub 限速与批量配置
├── pipeline/
│   ├── db.py              # 主库 schema 迁移（幂等）+ repository URL 解析 + 图标源映射
│   ├── seed.py            # 种子导入：dsh.works JSON → SQLite（清洗+中英拆分+分类）
│   ├── incremental.py     # 增量：git commit 补齐 + git 版本号 + npm 更新检测 + 图标探测 + topic 扫描
│   └── build.py           # SQLite → dist/index.json（format-1）+ 网站数据
├── site/index.html        # 网站源码（build 时合并进输出）
├── data/                  # raw/ 原始种子（gitignore）；store.db SQLite 主库（Actions 缓存持久化）
├── dist/                  # 构建产物（index.json + 网站，不入库）
├── .github/workflows/     # build-deploy.yml：全自动流水线
└── deploy/                # 本地 GCP/Caddy 部署脚本（备选，现主要用 Pages）
```

## 提交插件（收录规则）

网站「提交插件」页生成预填 Issue：https://github.com/qianyumeng0228/dsh-store/issues/new

1. **提交方式**：本仓库 GitHub Issue（表单自动生成预填链接）。
2. **收录条件**：仓库存在且 `package.json` 含 `dsh` 字段（管道自动校验证据），或已在 npm 发布且能被 dsh 识别；GitHub 仓库建议打 `topic:dsh-plugin`。
3. **收录来源**：npm 包名（`dsh plugin add <name>`）或 `github:owner/repo`（`dsh plugin add github:<owner/repo>`）。
4. **处理流程**：提交后由管道每日自动核验，通过即进入索引（状态 verified）；需人工判断的会挂起。
5. **安全提示**：git 来源在 DSH 中安装可能需要 allowBuilds，建议锁定 commit；恶意、盗用或失效插件会被移除。

## 使用（给 DSH 用户）

**加索引源**：DSH → Plugins → Discover → Index sources，粘贴：

```
https://qianyumeng0228.github.io/dsh-store/index.json
```

**命令行安装**：

```sh
dsh plugin add <name>                    # npm 插件
dsh plugin add github:<owner/repo>       # git 插件
```

## 运维

云端全自动（GitHub Actions，每天 03:17 UTC + 每次 push），无需手动操作。本地复跑：

```powershell
python pipeline/seed.py                 # 种子刷新
python pipeline/incremental.py --all    # 补 commit + git 版本号 + npm 更新检测 + 图标探测 + 扫新仓库（本地无 token 时每轮 40 个）
python pipeline/incremental.py --updates --npm-batch 500 --git-batch 200   # 本地只想小范围试更新检测时
python pipeline/build.py                # 生成索引与网站
```

`store.db` 是增量成果的唯一载体：云端由 `actions/cache` 按运行编号跨运行累积（缓存键必须每轮唯一，否则命中旧键后不会上传，成果全部丢失）。

## 已验证

- [x] `index.json` 符合 format-1（format/title/metadata.categories/plugins[] 结构与现有 vlln、dshopen 索引一致）
- [x] 输出条目必填字段零缺失，名称唯一非空，可装条目 `versions[0].version` 均为非空字符串
- [x] 新字段（author/avatar/icon/dsh/latestVersion/hasUpdate）全部进入管道输出与网站数据，build 幂等（连跑两次指纹一致）
- [x] 作者头像/图标为真实数据（抽查 dsh-im：作者 xmanrui、头像 URL 实测 200、最新版 5.9.1 > 收录 5.9.0 正确标记有新版本）
- [x] 网站渲染：头像 / 图标 / 🔄 有新版本 / DSH 兼容过滤 / 提交插件 / 横滑分区 / 分类导航 / 搜索 / 排序 / 筛选 / 详情弹层 / 待装栏（本地 fixture + 真实数据双验证）
- [x] 本地 http 服务 200，`index.json` 与 `data/plugins.json` 可访问且为合法 JSON
- [x] GitHub Pages 线上：首页 200（text/html）、索引与数据 200
- [x] Actions 流水线：seed → incremental → build → deploy 全自动跑通（含 BOM/限速/失败率熔断容错）
- [x] 8 条 fixture 用例覆盖 build 输出：npm_meta 直接补版本（不打 registry）、404 包不再重复请求、跨仓库撞名两条都保留、同仓库重复仍合并、git 安装命令用完整 owner/repo、hasUpdate 判定
- [x] `http_get_json` 429 自动降级 npmmirror 并取到版本，404 抛 HTTPError（此前 NameError 被吞导致降级整段失效）
- [x] 网站在浏览器实测：静态站（无宿主 API 时降级为复制命令、默认列表视图、视图选择持久化、屏蔽/收藏/待装/已装联动、顶部"全部更新"计数随标记实时变化）与宿主模式（已装识别含按 repo 匹配的 git 插件、安装/更新载荷、iframe 内只保留宿主一条滚动条）两条路径
- [x] `deploy/deploy.ps1` 可执行（此前 `param([string]$Host)` 与 PowerShell 只读自动变量冲突，一运行即 VariableNotWritable）
- [x] git 条目真实版本号：`--gitver` 从锁定的 commit 读 `package.json#version`（含 monorepo 子目录），索引不再恒写占位 `0.0.0`；只在 commit 变化时重读
- [ ] git 插件 commit 全量补齐：缓存修复后每天 2,000 个开始真正累积
- [x] 差异追平与探测节流分列：`git_probed` 负责"7 天内不重复探测"，`git_checked` 只记差异起始日；追平改成一条纯 SQL（不花 API 配额）。此前两者挤在一列，无差异的高星仓库每轮立刻重新可选、占满 batch，长尾仓库永远轮不到
- [x] `scan_new` 落库补全：bundle 识别（旧代码写的是 `"dsh.bundle" in data`，只匹配字面带点的键名，永远命中不了）、按 topics 映射 `category_key`（此前硬编码 `tools`）、把探测到的子目录写进 `path`、`INSERT OR IGNORE` 撞名不再计入新增数
- [x] 索引字段对齐参考实现：`versions[].engines`（dshopen 979/979 都有）、npm `source.registry`（938/938 都有）、monorepo `source.path`
- [ ] 图标 / 下载量 / npm engines.dsh 同理逐日累积（`NPM_UPDATE_BATCH=2000` ⇒ 7,073 个包约 3.5 天轮完一圈）

## 已知边界

- **"有新版本"的语义**：本站是目录站，标记 = 与本站上次收录相比发布了新版本/新提交（7 天内自动追平收录版本）；**无法读取用户本机已装版本**。真实的一键安装/全部更新/进度条/重启提醒/导出日志/打开配置文件/任务面板/评论计数是宿主内 dsh-market 的能力，本站以"手动标记已安装 + 待装清单 + 复制命令"替代，高级页"关于"有完整说明。
- **展示名撞名规则**：同一展示名下出现多个不同仓库时，用仓库名命名的条目会加 `owner-` 前缀（npm 包名本身全局唯一，不加前缀）。此前实现按展示名去重且只保留 star 最高的一个，实测丢弃了 3,436 条真实插件。
- 索引体积约 10.9MB（17,447 条全字段）；DSH 15 秒加载窗口在 Pages CDN 下足够，若要更小可后续生成精简索引。
- `index.json` 入库是为了 jsDelivr 镜像，每轮最多 ~11MB；仓库体积随时间线性增长，需要时改用 Release 资产或孤儿分支存放。
- `dsh.works` 的 21,928 条含 skill/theme/tool 类别，当前索引只收 plugin/bundle；技能包后续可按需放开。
- 分类由 tags 映射，无 tag 命中的插件落入「工具与能力」兜底。
- 作者头像依赖 GitHub 头像 CDN（`github.com/<owner>.png`），网络受限时可能加载失败（前端已做 onerror 自动隐藏，不影响浏览）。
- 定时构建不保证准点：`03:17 UTC` 的 schedule 曾延后到 10:32 才起跑，个别日子未触发。需要严格时效请改 `workflow_dispatch` 外部触发。
- `site/index.html` 是 **本仓库为唯一来源**，`dsh-store-market` 内的同名副本由其 `sync-upstream` 工作流同步；改 UI 只改这里，宿主专用差异（iframe 不滚动）由页面运行时检测，不再靠两份文件各自维护。
