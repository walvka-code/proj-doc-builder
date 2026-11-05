---
name: proj-doc-builder
description: >
  Safely scan, validate, and synchronize AI-readable project documentation,
  manifest v2, the CLAUDE document-tree index, and committed scan state. Use
  when the user asks to sync, refresh, generate, or validate project docs.
  也是「文档同步手册」：`project_docs/` 的定位（索引而非权威）、权威顺序、
  以及「发现漂移要登记而不是粉饰」的纪律。
  Triggers: "同步文档", "project_docs", "文档树", "CLAUDE.md", "generate docs",
  "sync docs", "validate docs", "文档漂移", "权威顺序", "索引不是权威"
version: "4.0"
disable-model-invocation: false
user-invocable: true
allowed-tools:
  - Bash
  - Read
  - Write
  - Glob
  - Grep
---

# proj_doc_builder v4.0 — 分阶段生成与确定性发布

> **路径约定（必读）**：下文 `<skill-dir>` 指本 `SKILL.md` 所在目录的**绝对路径**——
> 技能加载结果里的 `Base directory for this skill` 就是它。所有命令都在**项目根**执行，
> 因此脚本一律写成 `<skill-dir>/tools/xxx.py`；路径含空格时必须保留示例中的双引号。
> 技能以 DSH bundle 安装时 `<skill-dir>` 在 profile 的包目录内，以项目技能安装时它是
> `<项目根>/.dsh/skills/` 下的同名目录——两种布局下本文命令都成立。

本 skill 分为两类工作：

1. **模型生成/更新文档正文**：只处理 scanner 选出的新增或变化源码目录。
2. **工具确定性收尾**：`sync_project_docs.py` 负责 staging、严格校验、manifest v2、CLAUDE 文档树、Phase 4 报告和 scan state 发布。

协调器不生成自然语言正文。不得跳过文档更新而直接提交 scan baseline。

## 强制安全约束

- 默认先 dry-run；只有所有闸门通过后才运行 coordinator `--apply`。
- Phase 4 默认只报告。不得自动删除文档，不得自动添加 archive 标注，不得把旧 `--auto-approve` 当作授权。
- quarantine 是独立的显式动作；一次普通 `/proj-doc-builder` 不自动执行它。
- pending Phase 4 journal 必须先通过显式 `--recover` 处理；report、apply、restore 和 coordinator 都不得绕过恢复闸门。
- 活动 Markdown 集合只排除根级 `project_docs/.quarantine（运行时生成，可能不存在）/`；`.lingma/` 和嵌套同名目录仍属于活动树。
- 所有文本严格 UTF-8；Python 文件首行必须是 `# -*- coding: utf-8 -*-`。
- manifest path 仅允许相对 POSIX 小写 `.md` 路径；禁止绝对路径、反斜杠、`..`、ADS、保留设备名和大小写冲突。
- 不跟随 symlink、junction 或 reparse point；路径身份不能证明时 fail closed。
- 不自动批量重写未变化历史文档的自然语言内容。
- 根 `CLAUDE.md` 只更新 `doc-tree-instruction` 自动区块；其余架构、命令、测试等复杂区块只校验 marker，不自动改写。
- 不执行版本控制提交（`svn commit` / `git commit`），也不创建版本控制元数据目录（如 `.git`）。

## 工具职责

| 工具 | 职责 |
|---|---|
| `<skill-dir>/tools/project_docs_contract.py` | 路径、UTF-8、严格 JSON、链接、manifest、锁和原子写的唯一契约 |
| `<skill-dir>/tools/scan_dirs.py` | 内容哈希目录快照；生成 candidate，不提前推进 committed baseline |
| `<skill-dir>/tools/compute_link.py` | 计算或验证单个/批量相对文档链接 |
| `<skill-dir>/tools/validate_doc.py` | standard / placeholder profile 文档质量检查，只报告不修改 |
| `<skill-dir>/tools/link_check.py` | 全库只读链接检查；**不生成 manifest** |
| `<skill-dir>/tools/normalize_markers.py` | 仅规范化可精确识别的历史损坏 marker |
| `<skill-dir>/tools/claude_doc_index.py` | 只从候选 manifest 生成 `doc-tree-instruction` |
| `<skill-dir>/tools/phase4_exit.py` | fail-closed 报告、显式 quarantine、receipt restore 和持久 journal recovery；不删除活动文档 |
| `<skill-dir>/tools/sync_project_docs.py` | staging、闸门、统一根锁、发布与回滚的唯一协调器 |
| `<skill-dir>/tools/load_docs.py` | 加载器（只读）：先校验 manifest 与文档完全一致，再输出工程概览或单份文档/单节——给新对话一个入口 |

详细规则见 `references/`。

## Phase 0：环境与输入预检

在项目根执行 Python 工具，并先强制 UTF-8：

```powershell
$env:PYTHONIOENCODING='utf-8'
```

确认：

- `project_docs/` 是项目根下的真实目录；
- 根 `CLAUDE.md` 是真实普通文件；
- 没有正在进行的其他 sync/quarantine/restore，也没有待恢复的固定 `prepared`/`ready` Phase 4 journal、legacy journal、publication temp、detached artifact 或 coordinator publish journal；
- 不读取或执行项目自有的计划/反馈目录（若存在）；文档读取方在运行时严格只读，不回写业务文件。

## Phase 1：首次 coordinator dry-run

```powershell
python "<skill-dir>/tools/sync_project_docs.py" . --dry-run --json
```

退出码：

| 退出码 | 含义 |
|---:|---|
| 0 | 所有闸门通过，可发布或已无变化 |
| 1 | 文档质量、链接或 Phase 4 闸门失败 |
| 2 | 输入、schema、路径、UTF-8、锁或运行错误 |
| 3 | 发布失败；查看 `publish` 的 commit/rollback 状态 |

从 JSON 读取：

- `scan.changes`：`new/modified/deleted/unchanged/unknown`；
- `scan.worklist.directories`：本轮需生成或复核的源码目录；
- `scan.worklist.scaffold`：包含推荐 Profile（standard/placeholder）、直接文件清单与预计算相对链接的结构化脚手架；
- `scan.candidate_generation_id`：candidate 绑定；
- `validation.blocking_failures`：变化文档的阻断错误；
- `validation.audit_failures`：未变化历史文档的非阻断审计；
- `links.broken`；
- `phase4` 分类；
- `planned_writes`。

scanner 使用直接普通文件 SHA-256；相同 size/mtime 的内容变化也会进入 `modified`。boundary 后代和其他 unknown scope 不得被当成删除依据。

## Phase 2：生成或更新变化文档

只处理 `scan.worklist.directories` 中的 `new` / `modified` 目录。优先参考 `scan.worklist.scaffold` 中预计算的 `recommended_profile` 与 `precomputed_links`（包含 parent/children/tests 的相对链接），降低多轮外部工具调用。目录到文档的映射为：

```text
源码目录: <root>/a/b/
文档路径: <root>/project_docs/a/b.md
```

### 2.1 读取与分析

- 读取真实直接文件和直接子目录；
- 提取实际类、函数、常量和依赖；
- boundary 目录只写本层概要，不递归其后代；
- 二进制文件只记录文件名、类型和用途，不伪造符号。

### 2.2 选择 profile

- **standard**：有实质源码、标准材料或有效子模块；概述 3–4 个实质句子，文件/子目录清单必须与磁盘一致。
- **placeholder**：空目录或只有无实质内容的初始化文件；允许 1–2 句概述，不要求虚构符号、依赖或长篇系统角色。

模板见 `references/templates.md`，校验规则见 `references/checklist.md`。

### 2.3 链接

不得手工计算 `../`：

```powershell
python "<skill-dir>/tools/compute_link.py" `
  --project-docs project_docs `
  --from <当前文档相对路径> `
  --to <目标文档相对路径>
```

共享 resolver 支持 inline、reference-style、HTML anchor、fragment、query、extensionless `.md` 和严格 URL decode。详情见 `references/linking.md`。

### 2.4 单文档校验

```powershell
python "<skill-dir>/tools/validate_doc.py" `
  <doc.md> --source-dir <source_dir> --strict --json
```

本轮变化文档的任何 FAIL 必须修复。未变化历史文档只审计和报告，不为通过校验而机械改写。

### 2.5 删除和 unknown

- `deleted` 只进入 Phase 4 分析；不得直接删除对应文档。
- `unknown`、boundary 后代和不可信路径不得生成"源码已消失"结论。
- 未注册文档、manifest 缺失项或大小写漂移必须由 coordinator/contract 精确报告。

### 2.6 全量文件清单审计（`--full` 模式强制步骤）

> **教训**：扫描器标记 `unchanged` 只意味着目录指纹（文件集合哈希）未变，**不代表文档内容与磁盘一致**。源码在两次同步之间新增文件后，基线指纹已包含新文件，增量扫描判 unchanged → 文档永远不会被触发重写 → 文件清单永久漏失。

在 `--full` 模式下，**不论扫描器分类如何**，必须对全部已有文档运行文件清单审计：

1. 对每个 `project_docs/*.md`（排除 `_*` 和 `index.md`）：
   - 从文档的 `## 文件清单` 表格提取已记录的文件名集合
   - 从对应源码目录提取磁盘直接文件集合
   - 比对差异：磁盘有但文档缺 = **漏文件**；文档有但磁盘无 = **幽灵条目**
2. 漏文件：按 2.1-2.4 流程提取关键信息并补入表格
3. 幽灵条目：从表格删除该行；如果源码目录已空且文档之前是 standard，降级为 placeholder
4. 同时检查概述中的文件计数是否与实际一致

审计脚本可参考 `references/audit_script.md` 中的 Python 模板。

## Phase 3：全局 dry-run 闭环

更新正文后反复运行：

```powershell
python "<skill-dir>/tools/sync_project_docs.py" . --dry-run --json
```

必须同时满足：

- `ok == true`；
- 变化文档 strict 校验 0 阻断；
- `links.broken_count == 0`；
- manifest v2 candidate 覆盖完整活动 Markdown 集合；
- CLAUDE 六组 marker 唯一、成对、不交叠；
- Phase 4 无安全错误；存在 quarantine candidate 时发布照常进行但 scan baseline 冻结（见 Phase 4）；
- source snapshot 在 staging 与发布前复核中保持一致；
- **`--full` 模式额外要求**：全部 366+ 份文档的文件清单与磁盘逐项核对一致（Phase 2.6 审计结果为 0 漏文件 / 0 幽灵条目）。

`link_check.py` 仅做只读链接校验：

```powershell
python "<skill-dir>/tools/link_check.py" --project-docs project_docs --json
```

不得把它描述为 manifest 生成器。

## Phase 4：report-first 退出机制

普通同步只读取 coordinator 内嵌的 Phase 4 report。也可独立运行：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --json
```

只有同时满足以下条件才可成为 quarantine candidate：

- 已在受验证 manifest 注册；
- 含精确 AUTO marker；
- 对应源码目录确认不存在；
- 使用共享 resolver 统计为零入站引用；
- 不位于 boundary/unknown scope；
- 路径、UTF-8、hash 和 generation 均可信。

“概述短”“内容为空”或“占位”不是处置理由。有引用的历史文档仅报告，工具不自动 archive。

若 report 存在 candidate，普通 sync 可继续发布文档、marker 修复、CLAUDE 文档树和 manifest，但不推进 scan baseline（警告 `scan-baseline-frozen`）。这一设计使解除入站引用的文档编辑得以发布，避免 coordinator 闸门与 Phase 4 manifest 一致性校验相互死锁。只有用户另行明确要求 quarantine 时才运行：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --apply --json
```

恢复 receipt：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --restore <RECEIPT_ID> --json
```

quarantine/restore 在第一次内容变更前排他发布不可变的 `.phase4-transaction.json` prepared journal；后镜像准备完成后再以 no-replace 方式发布不可变的 `.phase4-transaction-ready.json` companion，绝不原地覆盖 prepared。manifest 通过 before/after anchor 执行 no-replace CAS，是唯一 commit point。restore 使用独立 transaction anchor 发布目标，原 quarantine payload 不与目标共享 inode。若进程被强制终止，或 journal/artifact 清理失败，所有普通动作和 coordinator 都会返回 `phase4-recovery-required`。此时必须先运行：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --recover --json
```

恢复只接受 journal 记录的精确 before/after 状态：manifest、活动文档、target/source、receipt、payload 或 CLAUDE 出现人工/并发变化时保留现场并 fail closed。无 pending journal 的 `--recover` 是成功 no-op。

详情见 `references/phase4.md`。

## Phase 5：确定性发布

所有闸门通过后：

```powershell
python "<skill-dir>/tools/sync_project_docs.py" . --apply --json
```

固定事务语义：

1. coordinator 在 staging 前、staging 后和发布锁内检查 pending Phase 4 journal；存在或无法验证时不推进任何基线。
2. 所有候选先在系统临时目录中完成校验；dry-run 不在项目树创建 staging、缓存或其他路径。
3. 发布时获取项目统一根锁并重新核对文档、CLAUDE、manifest、scan state 和源码 snapshot。
4. 在系统临时目录的项目锁域旁排他发布持久 coordinator publish journal；该 journal 不污染版本控制工作副本。
5. 先发布精确 marker 修复和 CLAUDE 文档树。
6. manifest 最后作为文档代际 commit point。
7. scan state 最后提交；失败时依据 manifest/scan/artifact 的实际安装状态报告 `NOT_INSTALLED`、`UNKNOWN` 或 `PARTIALLY_INSTALLED`，不虚构已提交状态。
8. manifest 前失败只回滚本事务仍拥有的候选内容；检测到并发修改时不覆盖并报告 rollback conflict。

若返回 `coordinator-recovery-required`，必须运行协调器自己的恢复入口：

```powershell
python "<skill-dir>/tools/sync_project_docs.py" . --recover --json
```

它与 `phase4_exit.py --recover` 是两个独立状态机：前者恢复普通 sync 发布，后者恢复 quarantine/restore。无 pending coordinator journal 时，`--recover` 是成功 no-op。

manifest schema v2 包含 `generation_id`、producer、docs root、total、depth counts、last synced，以及每份文档的 path/depth/title/status/bytes/SHA-256/synced_at。v1 仅内存兼容；未知 schema 直接拒绝。

## Phase 6：终验与幂等

发布后再次运行同一 dry-run：

```powershell
python "<skill-dir>/tools/sync_project_docs.py" . --dry-run --json
```

预期：

- `planned_writes == []`；
- manifest `changed == false`；
- scan `commit_needed == false`；
- 0 断链；
- 无未解释的缺失、未注册、候选或 unknown 错误；
- **`--full` 模式额外要求**：重新运行全量文件清单审计脚本，确认全部文档与磁盘一致（与 Phase 2.6 使用同一脚本）。

零变化运行不得刷新 `last_synced`、manifest、CLAUDE 或 scan state。

最后只读执行版本控制范围检查；不提交（Git 与 SVN 各有等价命令）：

```powershell
git status
git diff
# SVN 仓库：svn status / svn diff
```

确认没有个人绝对路径、版本控制元数据文件、非预期自然语言重写或 quarantine 动作。

## 完成报告

报告至少包含：

- scanner 分类和处理目录数；
- 新建/更新文档路径；
- **文件清单审计结果**：审计的文档总数、漏文件修复数、幽灵条目清除数、空目录文档降级数；
- strict 与历史审计结果；
- marker 精确修复数；
- manifest schema/generation/total/depth counts；
- CLAUDE 文档树是否变化；
- 链接数量与断链数；
- Phase 4 candidates/blocked/missing/unregistered，以及 pending/recovered journal 状态；
- publish commit point、scan state 和 rollback/partial 状态；
- 第二次 dry-run 是否完全无变化；
- 版本控制 status/diff 范围；
- 明确说明未执行版本控制提交、未自动删除或 quarantine 文档。

## 文档定位与漂移纪律

## 定位
`project_docs/` 是**自动生成的目录镜像索引**，由本技能生成与校验
（工具在本技能自带的 `tools/` 子目录里，**不在项目根的 `tools/`**）。

## 权威顺序（冲突时按此采信）
1. **实测运行输出**（报告 / 日志 / 器件回读）
2. **标准原文**（你的标准与规范文档目录）
3. **源码**（被测代码本体）
4. **配置数据**（运行时配置、环境参数、设备描述）
5. `project_docs/`（索引）
6. `CLAUDE.md`（总览，可能滞后）
7. 技能文档（方法论，不替代 1–4）

## 漂移纪律
* 发现文档与实测不符：**先取证，再改文档，并在台账留痕**
* 不要"顺手改文档让它看起来对"；不要把历史计划文档改写成事后正确
* 已知漂移应显式列出（例如：标称值 vs 实测标定值、目录迁移后的旧路径）

## 何时不要动文档
* 正在跑测试批次（改动会污染归档）
* 结论尚无法复现（先补证据）

## 收尾自检
```bash
python -m pytest "<skill-dir>/tests" -q    # 本技能自带的用例（Windows 上部分用例按平台跳过）
```
