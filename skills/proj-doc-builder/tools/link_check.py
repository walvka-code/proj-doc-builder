# -*- coding: utf-8 -*-
"""严格校验 ``project_docs`` 内的 Markdown 文档链接。

本工具只校验和报告，不生成或写入 manifest。manifest 发布由
``sync_project_docs.py`` 的最终协调阶段负责。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from project_docs_contract import (
    LinkDisposition,
    PathConflictError,
    PathValidationError,
    ProjectDocsContractError,
    configure_stdio_utf8,
    discover_active_markdown_paths,
    read_markdown_links,
    resolve_docs_root,
    stable_json_dumps,
)


def discover_documents(project_docs: str | os.PathLike[str]) -> list[tuple[str, Path]]:
    """使用共享契约确定性枚举完整活动 Markdown 集合。"""

    root = resolve_docs_root(project_docs)
    return [
        (relative, root.joinpath(*PurePosixPath(relative).parts))
        for relative in discover_active_markdown_paths(root)
    ]


def check_links(
    project_docs: str | os.PathLike[str],
) -> tuple[int, list[dict[str, Any]]]:
    """检查全部本地文档链接，返回 ``(问题数, 详情)``。"""

    root = resolve_docs_root(project_docs)
    docs = discover_documents(root)
    issues: list[dict[str, Any]] = []

    def _check_doc(doc_rel: str) -> list[dict[str, Any]]:
        doc_issues: list[dict[str, Any]] = []
        for link in read_markdown_links(doc_rel, root):
            if link.disposition is LinkDisposition.SKIPPED:
                continue
            if link.disposition is LinkDisposition.INVALID:
                doc_issues.append(
                    {
                        "doc": doc_rel,
                        "text": link.text,
                        "target": link.raw_target,
                        "resolved": link.normalized_target,
                        "kind": "invalid",
                        "reason": link.reason,
                    }
                )
            elif link.exists is False:
                doc_issues.append(
                    {
                        "doc": doc_rel,
                        "text": link.text,
                        "target": link.raw_target,
                        "resolved": link.project_relative_path,
                        "kind": "missing",
                        "reason": link.reason,
                    }
                )
        return doc_issues

    if len(docs) > 1:
        max_workers = min(32, (os.cpu_count() or 4) * 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            doc_rels = [doc_rel for doc_rel, _ in docs]
            for doc_issues in executor.map(_check_doc, doc_rels):
                issues.extend(doc_issues)
    else:
        for doc_rel, _ in docs:
            issues.extend(_check_doc(doc_rel))

    return len(issues), issues


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验 project_docs Markdown 链接")
    parser.add_argument(
        "--project-docs",
        default="project_docs",
        help="project_docs 目录（默认: project_docs）",
    )
    parser.add_argument("--json", action="store_true", help="stdout 仅输出 JSON")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="兼容旧参数；当前工具始终只执行链接检查",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="已废弃；manifest 只能由同步协调器发布",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio_utf8()
    args = _parser().parse_args(argv)
    if args.manifest_only:
        message = "--manifest-only 已废弃；请使用 sync_project_docs.py 构建 manifest"
        if args.json:
            print(stable_json_dumps({"error": message, "ok": False}), end="")
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        return 2

    try:
        issue_count, issues = check_links(args.project_docs)
        result = {
            "ok": issue_count == 0,
            "broken_count": issue_count,
            "broken": issues,
            "manifest_written": False,
        }
        if args.json:
            print(stable_json_dumps(result), end="")
        elif issue_count:
            print(f"发现 {issue_count} 条无效或断开的文档链接:")
            for issue in issues:
                print(
                    f"  {issue['doc']} -> {issue['target']} "
                    f"({issue['kind']}: {issue['reason']})"
                )
        else:
            print("文档链接校验通过：0 条断链。")
        return 1 if issue_count else 0
    except (OSError, ProjectDocsContractError, PathConflictError) as exc:
        if args.json:
            print(stable_json_dumps({"error": str(exc), "ok": False}), end="")
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
