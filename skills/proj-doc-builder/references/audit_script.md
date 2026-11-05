# 全量文件清单审计脚本参考

> Phase 2.6 / Phase 6 使用的审计脚本模板。Agent 可基于此编写 Python 脚本，也可自行实现等效逻辑。

## 脚本功能

对 `project_docs/` 下的每个 `.md` 文件（排除 `_*` 和 `index.md`）：

1. 从 `## 文件清单` 表格提取已记录的文件名集合（`| \`filename\` |` 模式）
2. 从对应源码目录提取磁盘直接文件集合
3. 比对差异并报告

## Python 模板

```python
# -*- coding: utf-8 -*-
"""全量文档文件清单一致性审计"""
import re
from pathlib import Path

def audit(project_root: str):
    root = Path(project_root)
    pd = root / 'project_docs'
    
    issues = []
    
    for md in pd.rglob('*.md'):
        # 跳过内部文件和索引
        if md.name.startswith('_') or md.name == 'index.md':
            continue
        
        # 计算对应源码目录路径
        rel = md.relative_to(pd)
        src_dir = root / rel.parent / rel.stem.replace('.md', '')
        
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        
        # 磁盘文件
        disk_files = sorted([
            f.name for f in src_dir.iterdir()
            if f.is_file() and not f.name.startswith('.')
        ])
        disk_set = set(disk_files)
        
        # 文档表格中的文件名
        content = md.read_text(encoding='utf-8')
        file_section = re.search(
            r'## 文件清单\s*\n(.*?)(?=\n## |\Z)', content, re.S
        )
        if file_section:
            doc_files = set(
                re.findall(r'^\| `([^`]+)`', file_section.group(1), re.M)
            )
        else:
            doc_files = set()
        
        missing = disk_set - doc_files
        extra = doc_files - disk_set
        
        if missing or extra:
            rel_str = str(rel).replace('\\', '/')
            issues.append({
                'doc': rel_str,
                'disk_count': len(disk_files),
                'doc_count': len(doc_files),
                'missing': sorted(missing),
                'extra': sorted(extra),
            })
    
    return issues


if __name__ == '__main__':
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else '.'
    issues = audit(root)
    
    if not issues:
        print('所有文档文件清单与磁盘一致 ✅')
    else:
        print(f'发现 {len(issues)} 个文档有问题:')
        for i in issues:
            print(f"  {i['doc']}: 磁盘 {i['disk_count']} / 文档 {i['doc_count']}")
            if i['missing']:
                print(f"    缺失: {i['missing']}")
            if i['extra']:
                print(f"    多余: {i['extra']}")
```

## 使用方式

```powershell
$env:PYTHONIOENCODING='utf-8'
python audit_doc_files.py "C:\path\to\project_root"
```
