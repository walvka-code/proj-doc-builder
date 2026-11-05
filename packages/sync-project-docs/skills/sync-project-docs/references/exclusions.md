# 扫描排除、边界与状态契约

`<skill-dir>/tools/scan_dirs.py` 只扫描应当生成项目文档的真实源码目录。扫描结果是 schema v2 candidate；只有协调器全部闸门通过后，candidate 才能成为 committed `_scan_state.json`。

## 排除目录

目录名使用 Unicode `casefold()` 比较。至少排除：

| 模式 | 原因 |
|---|---|
| `.svn/` `.git/` | 版本控制元数据（SVN 与 Git 均排除） |
| `.venv/` `__pycache__/` `.pytest_cache/` `.mypy_cache/` `.hypothesis/` | Python 环境与缓存 |
| `node_modules/` `dist/` `build/` `Debug/` `test_reports/` | 依赖或构建产物 |
| `.dsh/` `.lingma/` `.deepseek/` `.vscode/` `.idea/` `.settings/` | AI/IDE 配置 |
| `project_docs/` | 输出目录，禁止自扫描 |
| `sub_plan/` | 项目约定的辅助规划目录 |

当前实现还会在递归前排除名称以 `.` 开头的目录和直接文件。若项目确实把源码放在隐藏目录中，必须先明确调整 scanner 契约及测试，不能在一次同步中临时绕过。

同一目录内若名称经 `casefold()` 后冲突，扫描失败，不在 Windows 大小写语义下静默合并。

上述规则只针对**源码目录 scanner**。活动 Markdown inventory 使用共享 contract 的另一套完整集合规则：只排除根级 `project_docs/.quarantine（运行时生成，可能不存在）/`；`project_docs/.lingma/` 与嵌套同名 `.quarantine/` 仍属于活动文档，必须进入 manifest、链接检查和 load 校验。根 quarantine 必须使用规范小写名称。

## 文件快照

每个直接普通文件记录：

- 相对文件名与 `file_type`；
- 字节数和 `mtime_ns`；
- 从已验证文件描述符读取内容后计算的 SHA-256。

因此即使文件大小和时间戳保持不变，内容变化仍会被归为 `modified`。symlink、junction、reparse point、非普通文件、读取期间身份变化或根外路径均 fail closed。

目录快照还覆盖：

- 直接子目录集合；
- 当前目录是否为 boundary；
- skipped/unknown scope；
- 用于检测新增、修改、删除、重命名和结构变化的稳定 fingerprint。

## 增量分类

candidate 同时包含：

- `new`
- `modified`
- `deleted`
- `unchanged`
- `unknown`

`directories` 是本轮需要处理的工作清单；`snapshot.directories` 始终是当前完整可信快照。worklist、分类、计数、总数、最大深度、boundary、skipped、mode 和 snapshot 必须相互一致，否则 candidate 被拒绝。

`--full` / `force_full` 完全忽略旧基线，当前可见目录全部归为 `new`，不得携带旧 `deleted` 或 `unknown` 分类。

## 边界目录

- 扫描根自身的 `CLAUDE.md` 不构成边界。
- 根以下目录中任意大小写形式的 `CLAUDE.md` 构成 boundary。
- boundary 目录自身需要文档，但 scanner 不进入其后代。
- boundary 后代属于 unknown scope，不得被 Phase 4 当成“源码已消失”。
- 同一目录出现多个 casefold 相同的 boundary 文件时 fail closed。

## candidate 与 committed state

- scanner 默认只生成 candidate，不得提前覆盖 committed `_scan_state.json`。
- `candidate_generation_id` 覆盖完整 candidate 语义和工作清单。
- 提交已有 candidate 时必须同时提供调用者已经接受的 `expected_candidate_generation_id`；不匹配即拒绝，防止候选被替换后推进基线。
- 协调器在文档、链接、Phase 4、CLAUDE 和 manifest 闸门通过后才提交 scan state。
- manifest 是文档发布 commit point；scan state 发布失败必须报告 partial commit，不能假称整次同步成功。
- 零变化运行不刷新 manifest 或 committed scan state，保证幂等。

持久状态使用 `root: "."`、项目根派生的 `project_identity` 和可移植文件名，不保存个人绝对路径。旧 v1 或旧绝对根 v2 状态只做保守迁移，迁移后的目录重新处理。

## 输出约束

- `--json` 时 stdout 只能包含一个严格 UTF-8 JSON 文档。
- 所有持久 JSON 使用稳定排序、尾部换行、同目录临时文件和原子替换。
- `--output state.json` 这类无父目录参数必须正常工作。
- scanner 不提交 SVN，不创建 Git 文件，也不处理 quarantine 内的文档。
