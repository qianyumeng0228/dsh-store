# DSH 插件商店

面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 的社区插件商店：**format-1 索引源 + 静态浏览站**。在 DSH 的 Plugins → Discover → Index sources 里加一行索引地址，即可浏览、搜索、一键安装；网站供日常逛和查。

## 当前数据（2026-10-31 首次构建）

| 指标 | 数值 |
|---|---|
| 索引收录（verified + plugin/bundle） | 17,799 |
| 其中可一键安装 | 6,637（npm 6,619 / git 18） |
| 种子来源 | dsh.works `plugins.json`（21,928 条，2026-10-31 更新） |
| 判定标准 | dsh.works 逐条核验的 `package.json#dsh` 字段证据 |
| 分类 | 17 个双语分类（工具与能力 / 记忆 / 界面增强 / 模型与账号接入 …） |

> git 来源插件（13,778 条）已入库、已验证，但缺安装凭据（默认分支 commit）。无 GitHub Token 时增量管道每轮补 40 个；提供 `GITHUB_TOKEN` 后每轮补 2,000 个，几轮即可全量可装。

## 项目结构

```
dsh-store/
├── config.py              # 路径 / 收录规则 / 分类映射 / GitHub 限速配置
├── pipeline/
│   ├── seed.py            # 种子导入：dsh.works JSON → SQLite（清洗+中英拆分+分类）
│   ├── incremental.py     # 增量：git commit 补齐 + topic:dsh-plugin 新仓库扫描
│   └── build.py           # SQLite → dist/index.json（format-1）+ 网站数据
├── data/                  # raw/ 原始种子；store.db SQLite 主库
├── dist/
│   ├── index.json         # format-1 索引（DSH 索引源）
│   └── site/              # 静态网站（含 index.json 副本，直接发布）
└── deploy/
    ├── Caddyfile          # 站点配置模板
    └── deploy.ps1         # 部署到 GCP（ssh 别名 gcp，/var/www/dsh-store）
```

## 使用（给 DSH 用户）

**加索引源**：DSH → Plugins → Discover → Index sources，粘贴：

```
https://<你的域名>/index.json
```

**命令行安装**：

```sh
dsh plugin add <name>                    # npm 插件
dsh plugin add github:<owner/repo>       # git 插件
```

## 运维（给你）

```powershell
# 1) 种子刷新 + 增量补齐（建议放服务器 cron，每小时）
python pipeline/seed.py
python pipeline/incremental.py --all     # 先 --commit-only 补凭据，再 --scan-new 扫新仓库

# 2) 重新生成索引与网站
python pipeline/build.py

# 3) 发布（本地）
deploy/deploy.ps1                        # scp 到 gcp:/var/www/dsh-store + Caddy 生效
```

**GITHUB_TOKEN**：环境变量注入后可把增量速度从 40/轮 提到 2,000/轮（GitHub API 认证限额 5,000 req/h）。

## 已验证

- [x] `index.json` 符合 format-1（format/title/metadata.categories/plugins[] 结构与现有 vlln、dshopen 索引一致）
- [x] 必填字段零缺失，17,799 条全部有名称/标题/描述/仓库
- [x] 网站渲染：分类导航 / 搜索 / 排序 / 只看可安装 / 卡片分页 / 详情弹层 / 安装命令复制
- [x] 本地 http 服务 200，`index.json` 与 `data/plugins.json` 可访问
- [ ] git 插件 commit 全量补齐（依赖 GITHUB_TOKEN，进行中）
- [ ] 线上部署（依赖域名，待确认）

## 已知边界

- 索引体积 9.4MB（17,799 条全字段）；DSH 15 秒加载窗口在静态站/CDN 下足够，若要更小可后续生成精简索引（去掉 description 双语冗余）。
- `dsh.works` 的 21,928 条含 skill/theme/tool 类别，当前索引只收 plugin/bundle；技能包后续可按需放开。
- 分类由 tags 映射，无 tag 命中的插件落入「工具与能力」兜底。
