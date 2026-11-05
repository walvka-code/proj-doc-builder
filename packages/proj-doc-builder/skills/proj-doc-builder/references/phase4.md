# Phase 4：report-first 文档退出与恢复契约

## 1. 目标与边界

Phase 4 只处理“源码目录已确认消失、但镜像文档仍存在”的情况。它不根据文档篇幅、概述句数、占位状态或内容质量推断文档无效。

默认行为严格只读：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --json
```

普通 `/proj-doc-builder` 只消费同一分析器产生的报告。Phase 4 不会在普通同步中自动 quarantine、自动删除或自动添加历史存档标注。

## 2. 可信输入

分析同时使用：

| 输入 | 可信条件 | 用途 |
|---|---|---|
| 源码目录 inventory | 完整扫描、无越界或间接路径，并识别嵌套 `CLAUDE.md` boundary | 证明对应源码目录存在、缺失或处于 unknown scope |
| 活动 Markdown inventory | 共享 contract 完整枚举 | 发现未注册、缺失、大小写漂移和链接来源 |
| `_manifest.json` | 严格 JSON、受支持 schema、路径和文件 hash 可验证 | 建立已注册文档及 generation 快照 |
| Markdown links | 共享 resolver | 计算精确入站引用 |

活动 Markdown 集合只排除根级 `project_docs/.quarantine（运行时生成，可能不存在）/`。`.lingma/` 和嵌套目录中的同名 `.quarantine/` 都属于活动树，必须进入 manifest、链接检查和 Phase 4 分析。根 quarantine 名称必须规范为小写 `.quarantine`。

活动树中的 symlink、junction、reparse point、非规范 `.MD`、非法 UTF-8、路径大小写冲突或无法完整枚举的目录都会导致 fail closed。

## 3. candidate 条件

文档只有同时满足以下条件才进入 `candidates`：

1. 路径已在受验证 manifest 中注册；
2. 文件 bytes/SHA-256 与 manifest 一致；
3. 文件包含独立一行的精确 marker：
   `<!-- AUTO-GENERATED v3.0: folder-doc START -->`；
4. 对应源码目录在完整可信 inventory 中确认不存在；
5. 不位于 boundary 后代或其他 unknown scope；
6. 共享 Markdown resolver 计算得到零入站引用；
7. 活动文档集合、路径、UTF-8、manifest generation 均可信。

以下情况只进入 `blocked` 或 `errors`，不得处置：

- 存在入站引用；
- 缺少精确 AUTO marker；
- 对应源码路径不可信；
- 位于 boundary 后代；
- manifest 缺失、损坏、未知 schema 或与活动集合不一致；
- 文档未注册、已注册文档缺失或大小写不一致；
- 任意本地 Markdown 目标不安全。

“概述短”“内容为空”“占位文档”不是 candidate 条件。

## 4. 显式 quarantine

只有用户针对当前处置另行明确授权后，才可执行：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --apply --json
```

`--apply` 在统一项目根锁内重新分析，并在首次移动前全量复核所有 candidate。所有 candidate 组成一个批事务；任意候选失效都会阻止整批提交。

成功后，每份文档移动到：

```text
project_docs/.quarantine（运行时生成，可能不存在）/<receipt-id>/
├── receipt.json
└── document/<原 manifest 相对路径>
```

活动文档不被删除；原始内容以相同 SHA-256 保存在 receipt payload 中。`receipt.json` 是闭合 schema，至少记录：

- `receipt_id`
- `original_path`
- `quarantined_path`
- `sha256`
- `reason`
- 原 manifest `generation`
- `created_at`
- `state: quarantined`
- `restored_at: null`

完成移动后重新构建 manifest v2，并确定性更新根 `CLAUDE.md` 的文档树自动区块。manifest 是唯一 commit point：

- manifest 仍为事务前快照：quarantine 尚未提交，恢复必须整体回滚；
- manifest 等于事务后快照：quarantine 已提交，恢复只能补齐 receipt/payload/CLAUDE 等后处理，不能把活动文档静默放回。

## 5. receipt restore

恢复一个 receipt：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . `
  --restore <RECEIPT_ID> --json
```

恢复前必须证明：

- receipt ID、目录、JSON、payload 路径和 SHA-256 均可信；
- receipt 状态为 `quarantined`；
- 原目标路径当前不存在；
- 当前 manifest 和活动文档集合无未解释变化；
- 目标路径不经过 symlink、junction 或 reparse point。

目标文件使用 `O_CREAT | O_EXCL` 创建，绝不覆盖现有对象。恢复事务依次准备目标、CLAUDE 后镜像、receipt 后镜像和 manifest 后镜像；manifest 提交后 receipt 状态为 `restored`。确认活动目标和 receipt 均已提交后，才移除 quarantine 中的重复 payload。

再次恢复已完成 receipt 是幂等 no-op，但仍会校验目标 hash 和 manifest 注册状态。发现目标、receipt 或 payload 被人工修改时拒绝恢复。

## 6. 持久事务 journal

quarantine 或 restore 的第一次内容变更前，工具会以排他创建方式写入一个持久 journal：

```text
project_docs/.quarantine/.phase4-transaction.json
```

新事务使用**固定单例槽位**（文件名不含 transaction-id）；旧版 `.phase4-transaction-<transaction-id>.json` 命名仅作恢复兼容保留。

journal 使用严格闭合 schema，包含：

- operation、transaction ID、创建时间和 `prepared` / `ready` phase；
- manifest 前镜像及准备完成后的后镜像；
- 根 `CLAUDE.md` 前后镜像；
- quarantine 的每个 source/receipt/staging 身份与内容镜像；或
- restore 的 target/receipt/payload 前后镜像。

镜像保存严格 UTF-8 文本及 SHA-256。journal 文件在继续事务前会重新读取并完整校验；同一项目只允许一个 pending journal。

journal 存在时，以下动作全部返回 `phase4-recovery-required`，且不得推进 manifest 或 scan baseline：

- Phase 4 report；
- 新的 `--apply`；
- 新的 `--restore`；
- `sync_project_docs.py` dry-run 或 apply。

必须先显式执行：

```powershell
python "<skill-dir>/tools/phase4_exit.py" . --recover --json
```

无 pending journal 时，`--recover` 成功返回 `no-pending-transaction`，不修改项目。

## 7. 恢复状态机

恢复始终在统一项目根锁内运行，并先比较当前 manifest 与 journal 的精确前/后字节镜像。**Phase 4 `--recover` 在进入自身 journal 恢复前先检查 coordinator pending publish 状态**（prepared/committed/rolled_back/temp/artifact-root residue）；若 coordinator 存在未完成事务，返回 `coordinator-recovery-required` 并拒绝继续。这防止 Phase 4 recovery 绕过 coordinator pending gate 修改文件。

| operation | 当前权威状态 | 恢复动作 |
|---|---|---|
| quarantine | manifest 为 before | 验证活动集合只处于允许的部分移动状态，整体恢复所有 source，安全清理事务目录 |
| quarantine | manifest 为 after | 保持活动 source 缺失，补齐精确 payload/receipt/CLAUDE，并完成提交后清理 |
| restore | manifest 为 before，target 缺失 | 取消未开始的 restore，保持 receipt/payload 为 before |
| restore | manifest 为 before，target 等于 journal after | 验证没有额外活动文档，生成并持久化 after metadata，然后 roll-forward |
| restore | manifest 为 after，target 等于 journal after | 补齐 receipt/CLAUDE，验证 manifest，清理重复 payload |

以下任何情况都返回 `recovery-conflict` 或 `recovery-journal-invalid`，保留 journal 供人工处理，不覆盖或删除未知内容：

- manifest 既不等于 before 也不等于 after；
- 活动文档出现额外路径或丢失非事务路径；
- target/source/payload/receipt 内容与允许镜像不同；
- transaction 目录包含未拥有文件或目录；
- journal 名称、schema、时间、路径、hash 或 operation 不一致；
- 多个 pending journal 同时存在。

恢复和正常回滚只删除经过精确内容与路径校验的事务自有文件。发现人工或并发写入时保留现场并 fail closed。

## 8. Windows rename 处理

staging receipt 目录发布为最终 receipt 目录时，Windows 可能短暂返回共享锁错误。实现只对 `WinError 5/32/33` 做有限次数指数退避重试，并且每次重试前都要求：

- 最终目录仍不存在；
- staging 目录仍是首次检查到的同一对象。

其他错误立即上抛；目标出现、staging 身份变化或重试耗尽都会 fail closed。不会用宽泛重试掩盖永久错误。

## 9. 兼容选项与退出码

- `--dry-run` 仅是 report-only 兼容别名；
- 旧 `--auto-approve A` / `A+B` 已废弃且始终只报告，不能授权写入；
- `--apply`、`--restore`、`--recover` 互斥。

| 退出码 | 含义 |
|---:|---|
| 0 | report 安全、动作成功，或 recovery 无事务可处理 |
| 2 | 安全前置条件不成立、pending recovery 或冲突，未获准继续 |
| 3 | 文件系统/锁/持久化操作失败；检查 journal 和结构化错误 |

`--json` 时 stdout 只输出一个稳定排序、严格 UTF-8、带尾部换行的 JSON 文档。

## 10. 禁止事项

- 不直接 `rm` / `del` 活动文档；
- 不按 A/B/C 等级自动删除或自动 archive；
- 不把旧 `--auto-approve` 视为授权；
- 不手工删除 pending journal 来“解除阻塞”；
- 不在 manifest 冲突时强制回滚覆盖当前文件；
- 不删除 transaction 目录中的未知内容；
- 不在 Phase 4 动作中提交到版本控制。
