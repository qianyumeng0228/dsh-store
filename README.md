# DSH 插件商店

面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的社区插件商店：**format-1 索引源 + 静态浏览站**。在 DSH 的 Plugins → Discover → Index sources 里加一行索引地址，即可浏览、搜索、一键安装；网站供日常逛和查。

**线上地址（GitHub Pages 自动部署）**：
- 网站：https://qianyumeng0228.github.io/dsh-store/
- 索引源：https://qianyumeng0228.github.io/dsh-store/index.json
- 仓库：https://github.com/qianyumeng0228/dsh-store

每次 push / 每天 03:17 UTC 由 GitHub Actions 自动执行：种子刷新 → git commit 补齐 → npm 版本/更新检测 → 图标探测 → topic 扫描 → 生成索引 → 发布 Pages，无需手动运维。

## 当前数据（2026-11-01 自动刷新）

| 指标 | 数值 |
|---|---|
| 索引收录（verified + plugin/bundle） | 17,799+（每天增量增长） |
| 其中可一键安装 | 6,637（npm 全量 + git 插件每天补 commit） |
| 种子来源 | dsh.works `plugins.json`（21,928 条，每日刷新） |
| 作者头像覆盖（真实 GitHub 头像） | 16,933（95.1%） |
| 插件图标（dshopen + awesome 双源，GitHub 路径探测每日增量补齐） | 631 |
| 有新版本标记（数据刷新周期内有新版/新提交） | 174 条实时检测 |
| DSH 适配版本区间（engines.dsh） | 1,124 |
| 判定标准 | dsh.works 逐条核验的 `package.json#dsh` 字段证据 + 自建 topic 扫描复核 |
| 分类 | 17 个双语分类（工具与能力 / 记忆 / 界面增强 / 模型与账号接入 …） |

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
│   ├── incremental.py     # 增量：git commit 补齐 + npm 版本/更新检测 + 图标探测 + topic 扫描
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
python pipeline/incremental.py --all    # 补 commit + npm 更新检测 + 图标探测 + 扫新仓库（本地无 token 时每轮 40 个）
python pipeline/build.py                # 生成索引与网站
```

## 已验证

- [x] `index.json` 符合 format-1（format/title/metadata.categories/plugins[] 结构与现有 vlln、dshopen 索引一致）
- [x] 必填字段零缺失，17,799 条全部有名称/标题/描述/仓库
- [x] 新字段（author/avatar/icon/dsh/latestVersion/hasUpdate）全部进入管道输出与网站数据，build 幂等（连跑两次指纹一致）
- [x] 作者头像/图标为真实数据（抽查 dsh-im：作者 xmanrui、头像 URL 实测 200、最新版 5.9.1 > 收录 5.9.0 正确标记有新版本）
- [x] 网站渲染：头像 / 图标 / 🔄 有新版本 / DSH 兼容过滤 / 提交插件 / 横滑分区 / 分类导航 / 搜索 / 排序 / 筛选 / 详情弹层 / 待装栏（本地 fixture + 真实数据双验证）
- [x] 本地 http 服务 200，`index.json` 与 `data/plugins.json` 可访问且为合法 JSON
- [x] GitHub Pages 线上：首页 200（text/html）、索引与数据 200
- [x] Actions 流水线：seed → incremental → build → deploy 全自动跑通（含 BOM/限速/失败率熔断容错）
- [ ] git 插件 commit 全量补齐（每天 2,000 个，进行中）
- [ ] 图标 GitHub 路径探测云端每日 400 个逐步补齐（本地网络对 raw.githubusercontent.com 超时，云端不受影响）

## 已知边界

- **"有新版本"的语义**：本站是目录站，标记 = 与本站上次收录相比发布了新版本/新提交（7 天内自动追平收录版本）；**无法读取用户本机已装版本**。真实的一键安装/全部更新/进度条/重启提醒/导出日志/打开配置文件/任务面板/评论计数是宿主内 dsh-market 的能力，本站以"手动标记已安装 + 待装清单 + 复制命令"替代，高级页"关于"有完整说明。
- 索引体积约 9.4MB（17,799+ 条全字段）；DSH 15 秒加载窗口在 Pages CDN 下足够，若要更小可后续生成精简索引。
- `dsh.works` 的 21,928 条含 skill/theme/tool 类别，当前索引只收 plugin/bundle；技能包后续可按需放开。
- 分类由 tags 映射，无 tag 命中的插件落入「工具与能力」兜底。
- 作者头像依赖 GitHub 头像 CDN（`github.com/<owner>.png`），网络受限时可能加载失败（前端已做 onerror 自动隐藏，不影响浏览）。
