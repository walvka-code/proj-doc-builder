# 历史文档迁移

正常同步只处理当前 `project_docs/` 结构。只有明确发现旧 `AUTO-GENERATED: folder-doc`（无版本号）文档时才进入迁移；不得把迁移当作每次同步的固定步骤。

## 原则

- 先盘点、后生成、再校验，旧文件不自动删除。
- 路径必须通过共享 contract 归一化并保持在 `project_docs/` 内。
- 迁移只升级可确定的结构字段和 marker，不批量改写自然语言语义。
- 新旧数量、来源映射或目标冲突无法唯一确定时 fail closed。
- Phase 4 默认只报告；迁移遗留文件也必须经过独立、显式的 quarantine 决策。

## 安全流程

1. 使用受限搜索定位旧 marker，排除版本控制、缓存、AI/IDE 配置和 `project_docs/.quarantine（运行时生成，可能不存在）/`。
2. 为每个旧文档建立唯一映射：旧路径、对应源码目录、新 manifest 相对路径和目标路径。
3. 读取严格 UTF-8，解析标题、概述、文件清单、依赖、系统角色和状态。
4. 生成成对的 `folder-doc START/END` marker、`DOC-PATH` 和导航；无法确定的字段保留原文并报告，不伪造。
5. 用 `compute_link.py` 重新计算本地文档链接，不手算 `../`。
6. 对每个候选运行 `validate_doc.py`，然后运行全库只读 `link_check.py`。
7. 运行 `sync_project_docs.py --dry-run --json`；只有所有闸门通过后才执行显式 `--apply`。
8. 若存在 Phase 4 pending transaction，先运行 `phase4_exit.py --recover --json`；若存在 coordinator publish journal，则运行 `sync_project_docs.py --recover --json`。两类恢复不可互相替代，也不得手工删除 journal。
9. 迁移后再次运行 dry-run，确认 manifest v2、CLAUDE 文档树和 scan state 幂等无变化。
10. 旧文件若仍存在，仅由 `phase4_exit.py` 报告；不得因“迁移已完成”直接删除。

## 禁止事项

- 不使用 Glob 数量相等来替代逐路径映射校验。
- 不覆盖已经存在且内容不同的目标文档。
- 不使用旧 `--auto-approve` 作为写入授权。
- 不把“概述短”“占位”作为删除或 quarantine 理由。
- 不执行版本控制提交；迁移完成后只读检查版本控制范围（`git status` / `svn status` 及对应 diff）。
