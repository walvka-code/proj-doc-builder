# -*- coding: utf-8 -*-
"""安全、确定地计算和校验 ``project_docs`` 文档链接。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from project_docs_contract import (
    JsonContractError,
    LinkDisposition,
    ProjectDocsContractError,
    atomic_write_json,
    compute_relative_doc_link,
    configure_stdio_utf8,
    read_json_strict,
    read_markdown_links,
    resolve_docs_root,
    stable_json_dumps,
)


def _docs_root(project_docs_dir: str | os.PathLike[str]) -> Path:
    return resolve_docs_root(project_docs_dir)


def _contract_doc_path(value: str | os.PathLike[str], root: Path) -> str | Path:
    """把旧 CLI 的 ``project_docs/...`` 写法转换为契约路径。"""

    raw = os.fspath(value)
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    if "\\" in raw:
        raise ValueError(f"文档路径必须使用正斜杠: {raw!r}")
    pure = PurePosixPath(raw)
    if pure.parts and pure.parts[0] == root.name:
        pure = PurePosixPath(*pure.parts[1:])
    return pure.as_posix()


def compute_relative_link(
    from_doc: str,
    to_doc: str,
    project_docs_dir: str = "project_docs",
    *,
    must_exist: bool = False,
) -> str:
    """计算两个受信文档之间的 POSIX 相对链接。"""

    root = _docs_root(project_docs_dir)
    return compute_relative_doc_link(
        _contract_doc_path(from_doc, root),
        _contract_doc_path(to_doc, root),
        root,
        must_exist=must_exist,
    )


def validate_doc_links(doc_path: str, project_docs_dir: str) -> list[dict[str, Any]]:
    """返回文档中的无效或不存在本地文档链接。"""

    root = _docs_root(project_docs_dir)
    source = _contract_doc_path(doc_path, root)
    broken: list[dict[str, Any]] = []
    for link in read_markdown_links(source, root):
        if link.disposition is LinkDisposition.SKIPPED:
            continue
        if link.disposition is LinkDisposition.INVALID or link.exists is False:
            broken.append(
                {
                    "text": link.text,
                    "target": link.raw_target,
                    "resolved": link.project_relative_path
                    or link.normalized_target
                    or "",
                    "kind": (
                        "invalid"
                        if link.disposition is LinkDisposition.INVALID
                        else "missing"
                    ),
                    "reason": link.reason,
                }
            )
    return broken


def batch_compute(
    pairs: Sequence[Mapping[str, Any]],
    project_docs_dir: str = "project_docs",
    *,
    must_exist: bool = False,
) -> list[dict[str, str]]:
    """批量计算 ``from``/``to`` 文档对。"""

    if isinstance(pairs, (str, bytes)):
        raise JsonContractError("批量输入必须是对象数组")
    results: list[dict[str, str]] = []
    for index, pair in enumerate(pairs):
        if not isinstance(pair, Mapping):
            raise JsonContractError(f"pairs[{index}] 必须是对象")
        from_doc = pair.get("from")
        to_doc = pair.get("to")
        if not isinstance(from_doc, str) or not isinstance(to_doc, str):
            raise JsonContractError(f"pairs[{index}] 必须包含字符串 from/to")
        results.append(
            {
                "from": from_doc,
                "to": to_doc,
                "link": compute_relative_link(
                    from_doc,
                    to_doc,
                    project_docs_dir,
                    must_exist=must_exist,
                ),
            }
        )
    return results


def _load_pairs(path: str | os.PathLike[str]) -> Sequence[Mapping[str, Any]]:
    value = read_json_strict(path)
    pairs = value.get("pairs") if isinstance(value, Mapping) else value
    if not isinstance(pairs, list):
        raise JsonContractError("批量 JSON 必须是数组或包含 pairs 数组的对象")
    return pairs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="计算并校验 project_docs 文档链接")
    parser.add_argument("--from", dest="from_doc", help="源文档路径")
    parser.add_argument("--to", dest="to_doc", help="目标文档路径")
    parser.add_argument("--validate", help="校验单个文档中的全部链接")
    parser.add_argument(
        "--project-docs",
        default="project_docs",
        help="project_docs 目录（默认: project_docs）",
    )
    parser.add_argument("--batch", help="批量输入 JSON")
    parser.add_argument("--output", "-o", help="批量结果输出文件")
    parser.add_argument(
        "--must-exist", action="store_true", help="计算时要求两端文档存在"
    )
    parser.add_argument("--json", action="store_true", help="仅向 stdout 输出 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio_utf8()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        modes = (
            int(bool(args.from_doc or args.to_doc))
            + int(bool(args.validate))
            + int(bool(args.batch))
        )
        if modes != 1:
            raise ValueError("必须且只能选择 from/to、validate 或 batch 一种模式")
        if bool(args.from_doc) != bool(args.to_doc):
            raise ValueError("--from 与 --to 必须同时提供")

        if args.from_doc:
            link = compute_relative_link(
                args.from_doc,
                args.to_doc,
                args.project_docs,
                must_exist=args.must_exist,
            )
            if args.json:
                print(stable_json_dumps({"link": link}), end="")
            else:
                print(link)
            return 0

        if args.validate:
            broken = validate_doc_links(args.validate, args.project_docs)
            result = {
                "document": args.validate,
                "broken_count": len(broken),
                "broken": broken,
                "valid": not broken,
            }
            if args.json:
                print(stable_json_dumps(result), end="")
            elif broken:
                print(f"发现 {len(broken)} 条无效链接: {args.validate}")
                for item in broken:
                    print(
                        f"  [{item['text']}]({item['target']}) -> "
                        f"{item['resolved']} ({item['reason']})"
                    )
            else:
                print(f"链接有效: {args.validate}")
            return 1 if broken else 0

        results = batch_compute(
            _load_pairs(args.batch),
            args.project_docs,
            must_exist=args.must_exist,
        )
        if args.output:
            written = atomic_write_json(args.output, results)
            if args.json:
                print(
                    stable_json_dumps(
                        {
                            "ok": True,
                            "count": len(results),
                            "written": written.as_posix(),
                        }
                    ),
                    end="",
                )
        elif args.json:
            print(stable_json_dumps({"results": results}), end="")
        else:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ProjectDocsContractError) as exc:
        if args.json:
            print(stable_json_dumps({"error": str(exc), "ok": False}), end="")
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
