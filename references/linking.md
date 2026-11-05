# 链接计算与校验规则

## 单一规则来源

`.dsh/skills/sync-project-docs/tools/project_docs_contract.py` 是文档路径和 Markdown 链接解析的唯一规则来源。`compute_link.py`、`link_check.py` 与 Phase 4 必须复用该契约，不得各自实现正则和路径归一化。

本地文档路径必须满足：

- 使用相对 POSIX 路径和正斜杠 `/`；
- 文档扩展名为小写 `.md`；extensionless 文档链接由工具补 `.md`；
- URL percent escape 按严格 UTF-8 解码；
- query 与 fragment 不参与文件定位；
- inline link、full/collapsed/shortcut reference-style link 和 HTML `<a href>` 使用同一 resolver；
- image 不计作文档引用，reference definition 本身不重复计数；
- 解析后的目标必须仍位于 `project_docs/` 内；
- 绝对路径、盘符、UNC、NUL、反斜杠、越界 `..`、symlink/junction 逃逸均视为无效；
- 外部 URI、同文档 anchor 和已知源码文件扩展名跳过；
- `.quarantine/` 不参与全库链接检查。

## 工具调用

不要手工计算 `../` 层级。

```bash
# 单个链接；允许目标尚未写入
python .dsh/skills/sync-project-docs/tools/compute_link.py \
  --project-docs project_docs \
  --from project_docs/src/core.md \
  --to project_docs/src/utils/common.md

# 要求两端文档已经存在
python .dsh/skills/sync-project-docs/tools/compute_link.py \
  --project-docs project_docs \
  --from src/core.md \
  --to src/utils/common.md \
  --must-exist

# 校验单个文档，stdout 仅输出 JSON
python .dsh/skills/sync-project-docs/tools/compute_link.py \
  --project-docs project_docs \
  --validate src/core.md \
  --json

# 批量计算；输出采用同目录临时文件 + os.replace 原子写
python .dsh/skills/sync-project-docs/tools/compute_link.py \
  --project-docs project_docs \
  --batch links.json \
  --output resolved.json

# 全库只读检查；不会生成或覆盖 manifest
python .dsh/skills/sync-project-docs/tools/link_check.py --project-docs project_docs --json
```

退出码：

- `0`：全部有效；
- `1`：发现无效或不存在的本地文档链接；
- `2`：输入、路径、编码或契约错误。

## 禁止的写法

| 禁止 | 原因 | 处理 |
|------|------|------|
| `[name](../../project_docs/x/y.md)` | 重复嵌入输出根且可能越界 | 用 `compute_link.py` 生成 |
| `[name](C:/project_docs/x.md)` | 盘符绝对路径不可移植 | 使用相对 POSIX 路径 |
| `[name](..\\x.md)` | 反斜杠规则在不同平台不一致 | 使用 `/` |
| `[name](../%FF.md)` | percent escape 不是严格 UTF-8 | 修正链接文本 |
| 手工按 basename 统计引用 | 同名文档会误计数 | 使用共享 resolver |

## 批量输入格式

```json
{
  "pairs": [
    {
      "from": "src/core.md",
      "to": "src/utils/common.md"
    }
  ]
}
```

批量 JSON 必须是严格 UTF-8，重复 key、`NaN` 和 `Infinity` 会被拒绝。
