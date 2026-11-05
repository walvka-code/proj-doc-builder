# -*- coding: utf-8 -*-
"""机械规范化可精确识别的历史 ``Last synced`` 损坏尾缀。"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from link_check import discover_documents
from project_docs_contract import (
    ProjectDocsContractError,
    atomic_write_utf8,
    configure_stdio_utf8,
    parse_last_synced_marker,
    project_sync_lock,
    read_project_document,
    resolve_docs_root,
    stable_json_dumps,
)

# 历史迁移 bug 只会在完整 marker 后追加字面量 `` v3.1 -->``。
# 使用逐行锚定，避免“修复”任何其他自由文本或不完整注释。
_DUPLICATE_V31_TAIL_RE = re.compile(
    r"^(?P<marker><!--[ \t]*Last[ \t]+synced[ \t]*:[^\r\n]*?-->)[ \t]+"
    r"v3\.1[ \t]*-->[ \t]*(?=\r?$)",
    re.IGNORECASE | re.MULTILINE,
)


class MarkerNormalizationError(ValueError):
    """marker 候选不满足可机械修复的精确前置条件。"""


@dataclass(frozen=True)
class MarkerChange:
    path: str
    line: int
    before: str
    after: str
    original_content: str
    normalized_content: str


def normalize_marker_content(
    content: str, *, path: str = "<memory>"
) -> tuple[str, list[dict]]:
    """只移除精确的重复 v3.1 尾缀，保留原 marker 时间值。"""

    changes: list[dict] = []

    def replace(match: re.Match[str]) -> str:
        marker = match.group("marker")
        if parse_last_synced_marker(marker) is None:
            raise MarkerNormalizationError(f"无法解析 Last synced marker: {path}")
        line = content.count("\n", 0, match.start()) + 1
        changes.append(
            {
                "path": path,
                "line": line,
                "before": match.group(0),
                "after": marker,
            }
        )
        return marker

    normalized = _DUPLICATE_V31_TAIL_RE.sub(replace, content)
    return normalized, changes


def collect_marker_changes(project_docs: str | Path) -> list[MarkerChange]:
    """安全枚举文档并收集全部候选，不执行写入。"""

    root = resolve_docs_root(project_docs)
    collected: list[MarkerChange] = []
    for relative, _ in discover_documents(root):
        original = read_project_document(root, relative)
        normalized, changes = normalize_marker_content(original, path=relative)
        for item in changes:
            collected.append(
                MarkerChange(
                    path=relative,
                    line=item["line"],
                    before=item["before"],
                    after=item["after"],
                    original_content=original,
                    normalized_content=normalized,
                )
            )
    collected.sort(key=lambda item: (item.path, item.line))
    return collected


def _unique_file_changes(changes: Sequence[MarkerChange]) -> list[MarkerChange]:
    unique: dict[str, MarkerChange] = {}
    for change in changes:
        previous = unique.get(change.path)
        if (
            previous is not None
            and previous.normalized_content != change.normalized_content
        ):
            raise MarkerNormalizationError(
                f"同一文档产生不一致的 marker 候选: {change.path}"
            )
        unique[change.path] = change
    return [unique[path] for path in sorted(unique)]


def apply_marker_changes(
    project_docs: str | Path,
    changes: Sequence[MarkerChange],
    *,
    lock_timeout: float = 10.0,
) -> list[str]:
    """在单一锁下发布；失败时尽力逐文件回滚。"""

    root = resolve_docs_root(project_docs)
    files = _unique_file_changes(changes)
    applied: list[MarkerChange] = []
    with project_sync_lock(root.parent, timeout=lock_timeout):
        try:
            for change in files:
                pure_path = PurePosixPath(change.path)
                target = root.joinpath(*pure_path.parts)
                if read_project_document(root, change.path) != change.original_content:
                    raise MarkerNormalizationError(
                        f"发布前文档已变化，拒绝覆盖: {change.path}"
                    )
                atomic_write_utf8(target, change.normalized_content)
                applied.append(change)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for change in reversed(applied):
                target = root.joinpath(*PurePosixPath(change.path).parts)
                try:
                    current = read_project_document(root, change.path)
                    if current != change.normalized_content:
                        rollback_errors.append(
                            f"{change.path}: 回滚冲突，文件已被并发修改"
                        )
                        continue
                    atomic_write_utf8(target, change.original_content)
                except Exception as rollback_exc:  # pragma: no cover - 双重 I/O 故障
                    rollback_errors.append(f"{change.path}: {rollback_exc}")
            if rollback_errors:
                raise MarkerNormalizationError(
                    f"marker 发布失败且回滚不完整: {exc}; " + "; ".join(rollback_errors)
                ) from exc
            raise
    return [change.path for change in files]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="规范化历史 Last synced marker 尾缀")
    parser.add_argument("--project-docs", default="project_docs")
    parser.add_argument("--apply", action="store_true", help="原子写入候选修改")
    parser.add_argument("--json", action="store_true", help="stdout 仅输出 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio_utf8()
    args = _parser().parse_args(argv)
    try:
        changes = collect_marker_changes(args.project_docs)
        applied_paths: list[str] = []
        if args.apply and changes:
            applied_paths = apply_marker_changes(args.project_docs, changes)
        result = {
            "ok": True,
            "candidate_count": len(changes),
            "candidate_file_count": len({change.path for change in changes}),
            "applied": bool(args.apply and changes),
            "applied_paths": applied_paths,
            "changes": [
                {
                    "path": change.path,
                    "line": change.line,
                    "before": change.before,
                    "after": change.after,
                }
                for change in changes
            ],
        }
        if args.json:
            print(stable_json_dumps(result), end="")
        else:
            action = "已规范化" if args.apply else "待规范化"
            print(
                f"{action}: {len(changes)} 处 / {len(set(c.path for c in changes))} 个文件"
            )
        return 0
    except (OSError, MarkerNormalizationError, ProjectDocsContractError) as exc:
        if args.json:
            print(stable_json_dumps({"ok": False, "error": str(exc)}), end="")
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
