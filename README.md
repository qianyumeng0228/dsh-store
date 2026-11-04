English | [中文](README.zh.md)

[![build](https://github.com/qianyumeng0228/dsh-store/actions/workflows/build-deploy.yml/badge.svg)](https://github.com/qianyumeng0228/dsh-store/actions/workflows/build-deploy.yml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![catalog](https://img.shields.io/badge/catalog-29,593_plugins-blue)](https://qianyumeng0228.github.io/dsh-store/)

# dsh-store

The **catalog** behind the DSH plugin stores: a Python pipeline that discovers, verifies and
publishes the community plugin list for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness),
plus a static browsing site and two machine-readable index sources.

| | |
|---|---|
| Site | https://qianyumeng0228.github.io/dsh-store/ |
| Index source (recommended) | `https://qianyumeng0228.github.io/dsh-store/index-installable.json` |
| Full index (includes not-yet-installable rows) | `https://qianyumeng0228.github.io/dsh-store/index.json` |
| Site data (what the page loads) | `https://qianyumeng0228.github.io/dsh-store/data/plugins.json` |
| Host-side store plugin | [qianyumeng0228/dsh-plug-store](https://github.com/qianyumeng0228/dsh-plug-store) |

Nothing needs to be run by hand: every push and a daily 03:17 UTC schedule re-run
seed → discovery → verification → build → Pages deploy.

## Current numbers

Measured from the live `data/plugins.json` on 2026-11-04:

| | |
|---|---|
| Catalogued plugins | 29,593 |
| One-click installable | 14,344 (npm 10,598 sources / git remainder) |
| Categories | 17, bilingual |
| Descriptions in Chinese | ~99% (build-time machine translation, source kept in `descEn`) |
| Coverage of every other public DSH index | 92–96% of each, and a superset in total |

The last row is the point of this repo: it merges the ecosystem's independent registries instead
of copying one of them.

## How an entry earns its place

An entry is listed only when the pipeline finds **evidence in the repository itself** — a
`package.json` carrying a `dsh` field, a `dsh.bundle` declaration, or a dependency on
`@deepseek-ai/dsh*`. Names and topics are hints, never the criterion, so plugins that do not start
with `dsh-` are included and mislabelled ones are not.

Discovery channels, all automated:

| Channel | What it contributes |
|---|---|
| `dsh.works` seed | the largest single registry, refreshed daily |
| `awesome-dsh-plugin.com` | curated registry + icon map |
| `dshopen.org` | curated registry + icon map |
| GitHub topic scan | `topic:dsh-plugin`, `topic:deepseek-harness`, `topic:dsh` |
| npm keyword crawl | `keywords:dsh-plugin`, `keywords:deepseek-harness` + manifest verification |
| External index mirrors | other marketplaces' published `index.json` files, kept as *candidates* |
| GitHub code search | fallback for repositories that carry the manifest signature but no topic |

External indexes are never trusted directly: their entries land as `status='pending'` candidates
and are promoted only after `package.json` verification. That is why the catalog can claim 92–96%
coverage of them without inheriting their dead links.

## Using it from DSH

**As an index source** — DSH → Plugins → Discover → Index sources, paste:

```
https://qianyumeng0228.github.io/dsh-store/index-installable.json
```

Use this one rather than `index.json`: the full index keeps entries that are catalogued but not
yet installable (the site needs them for totals and categories), while the host's Discover renders
every row as installable — pasting the full index gives you thousands of rows that do nothing.
The installable file is also smaller: ~8.7 MB raw, ~1.8 MB gzipped, inside the host's 15-second
load window.

**From the command line:**

```sh
dsh plugin add <name>                 # npm-sourced plugin
dsh plugin add github:<owner/repo>    # git-sourced plugin
```

## Repository layout

```
dsh-store/
├── config.py              # paths, inclusion rules, category map, per-run batch limits
├── pipeline/
│   ├── db.py              # idempotent schema migration, repository URL parsing, icon maps
│   ├── seed.py            # seed import: dsh.works JSON → SQLite (cleaning, zh/en split, categories)
│   ├── discover.py        # discovery channels: external indexes, code search, npm keywords
│   ├── incremental.py     # commit fill, version reads, update checks, icon probe, topic scan, discovery
│   ├── translate.py       # batch machine translation of English-only descriptions
│   └── build.py           # SQLite → dist/index.json (format-1), index-installable.json, site data
├── site/index.html        # the store UI — single source of truth, copied into the plugin repo
├── index.json             # jsDelivr mirror of the latest full index (committed by CI)
├── data/                  # raw seeds and store.db (gitignored; the DB persists via Actions cache)
├── dist/                  # build output (gitignored)
└── .github/workflows/     # build-deploy.yml: the whole pipeline, unattended
```

`site/index.html` here is the **only** copy that should be edited; the plugin repo keeps a
byte-identical copy, and host-specific behaviour is detected at runtime inside the same file.

## Running it locally

```sh
python pipeline/seed.py                 # refresh the seed
python pipeline/incremental.py --all    # discovery + commit fill + versions + updates + icons
python pipeline/build.py                # emit indexes and site data
```

`GITHUB_TOKEN` matters: without it the API budget is 60 requests/hour and each stage degrades to
a small batch. `store.db` is the only carrier of incremental progress — in CI it is persisted by
`actions/cache` under a **per-run key** with `restore-keys`, because a cache key cannot be
overwritten once it exists (a fixed key silently froze the database at its first-day snapshot and
threw away every daily gain).

## Submitting a plugin

Use the site's **Submit** tab, which builds a pre-filled issue for this repository, or open one
directly: https://github.com/qianyumeng0228/dsh-store/issues/new

Inclusion requires a reachable repository whose `package.json` carries DSH evidence, plus either a
published npm package or a git source the host can install. Entries are re-checked daily; dead,
stolen or mislabelled ones are dropped. Adding `topic:dsh-plugin` to your repository gets it
picked up without a submission.

## Known limits

- **Git long tail.** A git-sourced entry needs a resolved commit before it becomes installable.
  ~17,000 entries are still queued and each run fills about 2,000 (API budget and runtime bound),
  so installable coverage keeps climbing for days rather than instantly.
- **"Has update" is catalog-relative.** This site cannot see your installed versions; it compares
  against what it last recorded. Per-machine update decisions belong to the host plugin.
- **Machine translation is machine translation.** Chinese descriptions are auto-generated from
  English ones and kept beside the source (`desc` / `descEn`) so the original is never lost.
- **The scheduled build is not guaranteed on time.** The 03:17 UTC cron has been observed starting
  hours late, or skipping a day. Use `workflow_dispatch` if you need a deterministic refresh.
- **`index.json` is committed** so jsDelivr can mirror it, which grows the repository by ~11 MB per
  meaningful change; if that ever hurts, it should move to release assets or an orphan branch.
- Listing is not endorsement: every entry is third-party code.

## License

MIT
