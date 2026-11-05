# sync-project-docs

**把工程变成 AI 读得懂的文档索引。** 一条命令生成大纲，一条命令加载概况。

> **语言**：[English](README.md)（默认）· **中文**
>
> 英文是默认版本；本文件为同步翻译，改内容时两版一起改。
>
> 同时提供 **DeepSeek Harness bundle**（`@walvka-code/sync-project-docs`）与普通 CLI 两种用法。

```bash
# 技能目录：SKILL.md 与 tools/ 都在这里
SKILL="$(pwd)/packages/sync-project-docs/skills/sync-project-docs"

# 同步：生成/更新文档索引（先看，不写盘 → 确认后发布）
python "$SKILL/tools/sync_project_docs.py" . --dry-run --json
python "$SKILL/tools/sync_project_docs.py" . --apply --json

# 加载：给新对话一个入口（概览 → 定位到某一份）
python "$SKILL/tools/load_docs.py" . --json
python "$SKILL/tools/load_docs.py" . --mode target --target src/core --json
```

它做的三件事：

1. 为**每个源码目录**维护一份 Markdown 大纲（文件清单 / 关键符号 / 依赖关系 / 系统角色）。
2. 用**内容哈希 + manifest** 记住"上次同步时的样子"，所以下一次只处理**真正变化**的目录。
3. 发布是**可回滚事务**；退役文档需要单独举证，不会自动删除。

一句话立场：**文档"已同步"必须由工具写下的 manifest 背书，而不是由生成它的模型自述。**

---

## 目录

- [安装](#安装)
- [仓库布局](#仓库布局)
- [它解决的两个问题](#它解决的两个问题)
- [核心保证](#核心保证)
- [工作流六阶段](#工作流六阶段)
- [数据模型](#数据模型)
- [加载器用法](#加载器用法)
- [平台支持](#平台支持)
- [测试](#测试)
- [限制与取舍](#限制与取舍)
- [License](#license)

---

## 安装

本仓本身是一个 **DeepSeek Harness bundle**，把技能、Python 工具和测试一起打包。按你的 agent 选一种：

**方式一：DSH bundle（推荐）**

在 **Plugins → More → 开启 Custom plugin markets → Discover** 里加入本仓的索引地址，然后安装
`sync-project-docs`：

```text
https://walvka-code.github.io/sync-project-docs/index.json
```

（同一份索引也发布在 `https://raw.githubusercontent.com/walvka-code/sync-project-docs/main/index.json`，
但部分网络无法直连该域名；以 GitHub Pages 地址为准。）

不用市场时，可直接钉住某个 commit（bundle 是仓库里的一个包目录）：

```bash
git+https://github.com/walvka-code/sync-project-docs.git#<40 位 commit>&path:/packages/sync-project-docs
```

或用 `plugin_manager` 装本地检出：

```json
{ "action": "install_bundle", "target": "C:/path/to/sync-project-docs/packages/sync-project-docs" }
```

bundle 挂载一个 bundled 技能提供方（rank 600）：技能在 profile 的**所有会话**里可用；若某个项目自带
`.dsh/skills/sync-project-docs/`，项目副本优先（用于钉住版本）。

**方式二：作为项目技能**

把 `packages/sync-project-docs/skills/sync-project-docs/` 复制到
`<project>/.dsh/skills/sync-project-docs/`。`SKILL.md` 用宿主报告的技能目录解析工具路径，
两种布局都成立。

**方式三：当普通 CLI 用**（不需要任何 agent 框架）

```bash
git clone <this-repo> sync-project-docs
SKILL="$(pwd)/sync-project-docs/packages/sync-project-docs/skills/sync-project-docs"
python "$SKILL/tools/sync_project_docs.py" /path/to/your/project --dry-run --json
```

**Python 3.12+，运行时零第三方依赖**（仅标准库）。测试需要 `pytest`（见 `requirements-dev.txt`）。

前置条件：你的项目根要有 `project_docs/` 与 `CLAUDE.md` 两个入口。

---

## 仓库布局

```text
sync-project-docs/
├── index.json                          # 插件市场索引（format 1）
├── packages/sync-project-docs/         # DSH bundle 包
│   ├── package.json                    # name / dsh.bundle.patch / engines
│   ├── cordis.patch.yml                # 挂载 bundled 技能提供方
│   ├── index.js                        # 读取 SKILL.md 并注册技能
│   └── skills/sync-project-docs/       # SKILL.md + tools/ + references/ + tests/
└── .github/workflows/ci.yml
```

---

## 它解决的两个问题

| # | 问题 | 本仓给出的答案 |
|---|---|---|
| **1** | 想快速梳理一个陌生工程，并留下"后续改了什么"的跟踪基线 | **大纲**：`project_docs/**/*.md`，每个源码目录一份。**基线**：`_scan_state.json`（逐文件 SHA-256 + 目录 fingerprint）与 `_manifest.json` 的 `generation_id`（只哈希内容）。两者合起来让下一次同步**只处理真正变化的目录**，而不是全量重写。 |
| **2** | 想让新开的 AI 对话快速加载工程概况，而不是每次从零读源码 | **加载器就在本仓**：`tools/load_docs.py` 先校验 manifest 与文档完全一致（逐份 hash + 集合完整），再输出**概览**（目录树 + 元数据 + 小节名）或**单份文档 / 单节**。写侧与读侧共用同一契约，不会各写一套解析而漂移。 |

为什么不是"让模型写一堆 md"：**生成者和校验者必须是两个执行体**，否则"模型说已同步"永远无法被证伪。

| 崩点 | 表现 | 本工具的应对 |
|---|---|---|
| 漂移 | 源码加了文件，文档"文件清单"永远少一行 | 内容哈希快照 + 变更工作清单，只重写变化的目录 |
| 自证不可信 | 模型说"已同步"，你无法复核 | 独立 manifest 记录每份文档的 size/SHA-256/title/status，不一致即拒绝 |
| 批量翻新毁历史 | 为过校验把所有历史文档重写一遍 | 只有"本轮变化"的文档是阻断项；历史文档只审计、不改写 |

---

## 核心保证

| 保证 | 机制 |
|---|---|
| **默认零写入** | 不带 `--apply` 一律只报告；staging 在系统临时目录，不在项目树建路径 |
| **单一提交点** | `project_docs/_manifest.json` 的那一次原子替换是文档代际的唯一 commit point |
| **不覆盖并发写入** | 发布用 no-replace rename（Windows `os.rename` / Linux `renameat2(RENAME_NOREPLACE)`），前置镜像不符即拒绝 |
| **失败必须分类** | 只报 `NOT_INSTALLED` / `INSTALLED` / `UNKNOWN` / `PARTIALLY_INSTALLED`，绝不虚构"已提交" |
| **不污染工作副本** | 锁、事务 journal、CAS 产物全在系统临时目录，版本控制里看不到 |
| **删除是举证而非清理** | 只在"源码目录确认消失 + 零入站引用 + 精确 marker + 已注册"时生成隔离候选，且默认只报告 |
| **幂等** | 零变化运行时 `planned_writes == []`，不刷新 `last_synced` |

---

## 工作流六阶段

| Phase | 动作 | 关键产物 |
|---|---|---|
| 0 | 预检：项目根、`project_docs/`、`CLAUDE.md`、无待恢复事务 | — |
| 1 | `--dry-run` 得到变化分类与工作清单 | `scan.worklist.scaffold`（推荐 profile + 预计算相对链接） |
| 2 | **生成方**按工作清单写正文并逐份 `--strict` 校验 | `project_docs/*.md` |
| 3 | 全局 dry-run 闭环（断链、阻断、manifest 覆盖面） | `validation.blocking_failures == []` |
| 4 | 退役报告：哪些文档的源码已消失且零引用 | `phase4.candidates`（默认只报告） |
| 5 | `--apply` 发布 | manifest / CLAUDE 文档树 / scan state |
| 6 | 终验 + 版本控制只读范围检查 | `planned_writes == []` |

**协调器不生成任何自然语言**：它只做校验与事务发布，正文由生成方（人或模型）写入。

退出码：`0` 全过 / `1` 闸门失败（含"存在待恢复事务"）/ `2` 输入或运行错误 / `3` 发布失败（**不等于未发布**，以 `publish.disposition` 为准）。

---

## 数据模型

| 文件 | 作用 |
|---|---|
| `project_docs/_manifest.json` | schema v2，每份文档记录 `path/depth/title/status/bytes/sha256/synced_at`，并给出 `generation_id`（只哈希内容，不含时间戳）与 `depth_counts` |
| `project_docs/_scan_state.json` | 源码目录快照（逐文件 SHA-256 + 目录 fingerprint）：**这是"改动跟踪基线"**，决定下一轮 worklist |
| `CLAUDE.md` 六组 marker | `doc-tree-instruction` / `project-overview` / `common-commands` / `architecture` / `test-structure` / `dependencies`。工具**只替换第一组**，其余仅校验 marker 唯一、成对、不交叠 |

---

## 加载器用法

```bash
# 概览：树 + 每份文档的 title/status/字节数/小节名（默认 80 份、每份 6 个小节名）
python "$SKILL/tools/load_docs.py" . --json
python "$SKILL/tools/load_docs.py" . --limit 0 --sections 0 --json   # 全部文档、只要元数据

# 定位：源码目录 / 文档路径 / 工程相对路径都可以
python "$SKILL/tools/load_docs.py" . --mode target --target src/core --json
python "$SKILL/tools/load_docs.py" . --mode target --target project_docs/src/core.md --json

# 只要某一节
python "$SKILL/tools/load_docs.py" . --mode target --target src/core --section 依赖关系 --json
```

**先校验再加载**：manifest 与磁盘文档不一致（被改过、或多出未注册文档）时退出 `1` 并报 `manifest-stale`，绝不在陈旧索引上服务。

退出码：`0` 已校验可服务 / `1` 索引陈旧或目标未注册 / `2` 用法或路径安全问题（含 `../` 越界）。

---

## 平台支持

| 平台 | 状态 | 说明 |
|---|---|---|
| **Windows** | ✅ 已验证 | 重解析点/junction 识别、句柄级删除（可回收墓碑）、`msvcrt` 锁 |
| **Linux** | ✅ 已验证 | `fcntl` 锁、`dir_fd` 逐级 `O_NOFOLLOW` 走查、`renameat2(RENAME_NOREPLACE)`、目录 `fsync` |
| **macOS** | ⚠️ 未验证 | 有 `renamex_np` 分支但无 CI；APFS 大小写不敏感与 `fsync` 语义差异未取证 |

Linux 上必须知道的三条：

1. **`renameat2(RENAME_NOREPLACE)` 是硬依赖。** 不支持时抛 `NoReplaceRenameUnsupported` 并**明确拒绝**回退到会覆盖的 `rename`——覆盖会静默破坏崩溃一致性。
2. **目录 `fsync` 被拒时降级不中断。** 跳过并显式暴露（`warnings.warn` + 报告 `durability-degraded`）；真实 I/O 错误仍上抛。
3. **POSIX 下墓碑只归档不删除。** 退役事务目录改名为 `.phase4-archive-*` 永久保留（无句柄绑定 unlink），需人工清理；Windows 可回收。

---

## 测试

```bash
python -m pytest packages/sync-project-docs/skills/sync-project-docs/tests -q
```

**360 个用例**（Windows：354 passed / 6 skipped；Linux 差异仅在于"Windows 句柄语义"用例反向跳过），全部使用 `tmp_path`，不触碰真实 `project_docs/`。锁定的不是"功能"而是**崩溃一致性**：

- manifest 之前失败必须完整回滚；之后失败只准报 `installed/partial/unknown`，禁止回滚
- `KeyboardInterrupt` 落在每一个 rename 前后的落点
- 伪造 journal 槽位、ABA 竞态、staging 期间文档被并发修改
- `renameat2` 不可用 → 类型化失败；目录 `fsync` 被拒 → 降级且可见（CI 有专门的容器/overlayfs 作业）

---

## 限制与取舍

- 事务 journal 与 CAS 产物目录必须与项目根**同一文件系统**（跨盘会让 `--apply` 直接拒绝）。
- 路径白名单：只接受相对 POSIX、小写 `.md`；拒绝绝对路径、盘符、反斜杠、`..`、ADS、保留设备名与大小写冲突。
- **不跟随** symlink / junction / reparse point；路径身份无法证明时一律拒绝。
- `--full` 只是扫描器的 `force_full` 开关，**不等于**"逐份文档盘点文件清单"。
- 文档退役是**举证流程**，不是清理工具：默认只报告，`--apply` 需另行明确授权。

---

## License

[MIT](LICENSE)
