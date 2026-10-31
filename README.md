# DSH 插件商店

面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的社区插件商店：**format-1 索引源 + 静态浏览站**。在 DSH 的 Plugins → Discover → Index sources 里加一行索引地址，即可浏览、搜索、一键安装；网站供日常逛和查。

**线上地址（GitHub Pages 自动部署）**：
- 网站：https://qianyumeng0228.github.io/dsh-store/
- 索引源：https://qianyumeng0228.github.io/dsh-store/index.json
- 仓库：https://github.com/qianyumeng0228/dsh-store

每次 push / 每天 03:17 UTC 由 GitHub Actions 自动执行：种子刷新 → git commit 补齐 → topic 扫描 → 生成索引 → 发布 Pages，无需手动运维。

## 当前数据（2026-10-31 首次构建）

| 指标 | 数值 |
|---|---|
| 索引收录（verified + plugin/bundle） | 17,799+（每天增量增长） |
| 其中可一键安装 | 6,637+（npm 全量；git 插件每天补 2,000 个 commit，约一周全量可装） |
| 种子来源 | dsh.works `plugins.json`（21,928 条，每日刷新） |
| 判定标准 | dsh.works 逐条核验的 `package.json#dsh` 字段证据 + 自建 topic 扫描复核 |
| 分类 | 17 个双语分类（工具与能力 / 记忆 / 界面增强 / 模型与账号接入 …） |

## 项目结构

```
dsh-store/
├── config.py              # 路径 / 收录规则 / 分类映射 / GitHub 限速配置
├── pipeline/
│   ├── seed.py            # 种子导入：dsh.works JSON → SQLite（清洗+中英拆分+分类）
│   ├── incremental.py     # 增量：git commit 补齐 + topic:dsh-plugin 新仓库扫描
│   └── build.py           # SQLite → dist/index.json（format-1）+ 网站数据
├── site/index.html        # 网站源码（build 时合并进输出）
├── data/                  # raw/ 原始种子；store.db SQLite 主库（Actions 缓存持久化）
├── dist/                  # 构建产物（index.json + 网站，不入库）
├── .github/workflows/     # build-deploy.yml：全自动流水线
└── deploy/                # 本地 GCP/Caddy 部署脚本（备选，现主要用 Pages）
```

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
python pipeline/incremental.py --all    # 补 commit + 扫新仓库（本地无 token 时每轮 40 个）
python pipeline/build.py                # 生成索引与网站
```

## 已验证

- [x] `index.json` 符合 format-1（format/title/metadata.categories/plugins[] 结构与现有 vlln、dshopen 索引一致）
- [x] 必填字段零缺失，17,799 条全部有名称/标题/描述/仓库
- [x] 网站渲染：分类导航 / 搜索 / 排序 / 只看可安装 / 卡片分页 / 详情弹层 / 安装命令复制
- [x] 本地 http 服务 200，`index.json` 与 `data/plugins.json` 可访问
- [x] GitHub Pages 线上：首页 200（text/html）、索引与数据 200
- [x] Actions 流水线：seed → incremental → build → deploy 全自动跑通（含 BOM/限速容错）
- [ ] git 插件 commit 全量补齐（每天 2,000 个，进行中）

## 已知边界

- 索引体积约 9.4MB（17,799+ 条全字段）；DSH 15 秒加载窗口在 Pages CDN 下足够，若要更小可后续生成精简索引（去掉 description 双语冗余）。
- `dsh.works` 的 21,928 条含 skill/theme/tool 类别，当前索引只收 plugin/bundle；技能包后续可按需放开。
- 分类由 tags 映射，无 tag 命中的插件落入「工具与能力」兜底。
