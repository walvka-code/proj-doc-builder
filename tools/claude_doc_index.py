# -*- coding: utf-8 -*-
"""从受验证 manifest 确定性生成根 ``CLAUDE.md`` 文档树区块。"""

from __future__ import annotations

import argparse
import copy
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, MutableMapping, Sequence

from project_docs_contract import (
    ManifestValidationError,
    ProjectDocsContractError,
    atomic_write_utf8,
    configure_stdio_utf8,
    load_manifest,
    project_sync_lock,
    read_utf8,
    stable_json_dumps,
)

MARKER_VERSION = "v3.0"
DOC_TREE_SECTION = "doc-tree-instruction"
REQUIRED_SECTIONS = (
    DOC_TREE_SECTION,
    "project-overview",
    "common-commands",
    "architecture",
    "test-structure",
    "dependencies",
)


class ClaudeIndexError(ValueError):
    """CLAUDE 自动生成区块不满足唯一、成对、有序契约。"""


def section_start(section: str) -> str:
    return f"<!-- AUTO-GENERATED {MARKER_VERSION}: {section} START -->"


def section_end(section: str) -> str:
    return f"<!-- AUTO-GENERATED {MARKER_VERSION}: {section} END -->"


def _marker_positions(content: str, section: str) -> tuple[int, int, int, int]:
    start = section_start(section)
    end = section_end(section)
    start_positions = [
        match.start() for match in re.finditer(re.escape(start), content)
    ]
    end_positions = [match.start() for match in re.finditer(re.escape(end), content)]
    if len(start_positions) != 1 or len(end_positions) != 1:
        raise ClaudeIndexError(
            f"CLAUDE section {section!r} markers must each occur once; "
            f"START={len(start_positions)}, END={len(end_positions)}"
        )
    start_pos = start_positions[0]
    end_pos = end_positions[0]
    if start_pos >= end_pos:
        raise ClaudeIndexError(f"CLAUDE section {section!r} END precedes START")
    return start_pos, start_pos + len(start), end_pos, end_pos + len(end)


def validate_required_sections(
    content: str,
    sections: Sequence[str] = REQUIRED_SECTIONS,
) -> dict[str, tuple[int, int, int, int]]:
    """校验需要保留的自动区块 marker，并拒绝区块交叠。"""

    positions = {section: _marker_positions(content, section) for section in sections}
    ordered = sorted(
        (bounds[0], bounds[3], section) for section, bounds in positions.items()
    )
    for (_, previous_end, previous), (next_start, _, following) in zip(
        ordered, ordered[1:]
    ):
        if next_start < previous_end:
            raise ClaudeIndexError(
                f"CLAUDE sections overlap: {previous!r} and {following!r}"
            )
    return positions


def _new_node(name: str) -> dict[str, Any]:
    return {"name": name, "entry": None, "children": {}}


def _manifest_tree(manifest: Mapping[str, Any]) -> dict[str, Any]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ManifestValidationError("manifest files must be an array")
    root = _new_node("project_docs")
    for index, raw_entry in enumerate(files):
        if not isinstance(raw_entry, Mapping):
            raise ManifestValidationError(f"files[{index}] must be an object")
        path = raw_entry.get("path")
        if not isinstance(path, str):
            raise ManifestValidationError(f"files[{index}].path must be text")
        pure = PurePosixPath(path)
        parts = list(pure.parts)
        parts[-1] = PurePosixPath(parts[-1]).stem
        node = root
        for part_index, part in enumerate(parts):
            children: MutableMapping[str, dict[str, Any]] = node["children"]
            folded = part.casefold()
            if folded not in children:
                children[folded] = _new_node(part)
            node = children[folded]
            # 历史文档文件名与对应源码目录可能只有大小写差异
            # （如 foo.md / Foo/）。它们是同一树节点，
            # 但完整 manifest path 并不重复；文档节点自身的 spelling 优先。
            if part_index == len(parts) - 1:
                node["name"] = part
        if node["entry"] is not None:
            raise ManifestValidationError(f"duplicate document tree node: {path!r}")
        node["entry"] = copy.deepcopy(dict(raw_entry))
    return root


def _node_label(node: Mapping[str, Any]) -> str:
    name = str(node["name"])
    entry = node.get("entry")
    if not isinstance(entry, Mapping):
        return name
    title = str(entry.get("title", "")).strip()
    status = str(entry.get("status", "❓"))
    title_name, separator, title_description = title.partition(" — ")
    if title_name.casefold() == name.casefold():
        display_name = title_name
        description = title_description.strip() if separator else ""
    else:
        display_name = name
        description = title
    suffix = f" — {description}" if description else ""
    return f"{display_name}{suffix} {status}".rstrip()


def _sorted_children(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    children = node.get("children", {})
    return sorted(
        children.values(),
        key=lambda child: (
            0 if str(child["name"]).casefold() in ("index", "index.md") else 1,
            str(child["name"]).casefold(),
            str(child["name"]),
        ),
    )


def _render_children(
    node: Mapping[str, Any],
    *,
    prefix: str,
    output: list[str],
) -> None:
    children = _sorted_children(node)
    for index, child in enumerate(children):
        last = index == len(children) - 1
        output.append(prefix + ("└── " if last else "├── ") + _node_label(child))
        _render_children(
            child,
            prefix=prefix + ("    " if last else "│   "),
            output=output,
        )


def render_document_tree(manifest: Mapping[str, Any]) -> str:
    """将 manifest 的每个文档渲染为唯一、完整的树节点。"""

    tree = _manifest_tree(manifest)
    lines = ["project_docs/"]
    _render_children(tree, prefix="", output=lines)
    return "\n".join(lines)


def render_doc_tree_section(manifest: Mapping[str, Any]) -> str:
    last_synced = manifest.get("last_synced")
    if not isinstance(last_synced, str) or not last_synced:
        raise ManifestValidationError("manifest last_synced must be non-empty text")
    producer = manifest.get("producer")
    if not isinstance(producer, Mapping):
        raise ManifestValidationError("manifest producer must be an object")
    producer_name = producer.get("name")
    producer_version = producer.get("version")
    if not isinstance(producer_name, str) or not isinstance(producer_version, str):
        raise ManifestValidationError("manifest producer name/version must be text")

    tree = render_document_tree(manifest)
    return "\n".join(
        [
            section_start(DOC_TREE_SECTION),
            f"<!-- Last synced: {last_synced} by {producer_name} v{producer_version} -->",
            "",
            "## 项目文档体系 (Documentation Tree)",
            "",
            "自动生成的项目文档集中在 `project_docs/`，目录结构镜像项目源码。",
            "每个 `.md` 文件描述对应目录的内容（文件清单、关键符号、依赖关系、系统角色等）。",
            "",
            "**使用方式**:",
            "- 从下方文档树中定位目标目录",
            "- 读取 `project_docs/` 下对应的 `.md` 文件",
            "- 每个 `.md` 顶部有面包屑导航，`## 依赖关系` 展示上下游模块",
            "- 叶节点 `.md` 包含对应目录的代码级详情",
            "",
            "### 文档树 (project_docs/)",
            "",
            "```",
            tree,
            "```",
            "",
            "状态: ✅ 完整 🔶 开发中 ⬜ 占位 📦 历史存档 ❓ 未知",
            "",
            section_end(DOC_TREE_SECTION),
        ]
    )


def replace_doc_tree_section(content: str, manifest: Mapping[str, Any]) -> str:
    """只替换 doc-tree 区块，保留其他 CLAUDE 内容逐字不变。"""

    positions = validate_required_sections(content)
    start_pos, _, _, end_after = positions[DOC_TREE_SECTION]
    newline = "\r\n" if "\r\n" in content else "\n"
    rendered = render_doc_tree_section(manifest).replace("\n", newline)
    return content[:start_pos] + rendered + content[end_after:]


def update_claude_doc_index(
    claude_path: str | Path,
    manifest: Mapping[str, Any],
    *,
    apply: bool = False,
    lock_timeout: float = 10.0,
) -> dict[str, Any]:
    """生成候选 CLAUDE 内容；显式 apply 在根锁内重新读取后发布。"""

    path = Path(claude_path).resolve(strict=True)

    def prepare() -> tuple[str, bool]:
        current = read_utf8(path)
        candidate = replace_doc_tree_section(current, manifest)
        return candidate, candidate != current

    if apply:
        with project_sync_lock(path.parent, timeout=lock_timeout):
            candidate, changed = prepare()
            if changed:
                atomic_write_utf8(path, candidate)
    else:
        candidate, changed = prepare()
    return {"path": path.as_posix(), "changed": changed, "content": candidate}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 CLAUDE.md 文档树区块")
    parser.add_argument("--claude", default="CLAUDE.md", help="根 CLAUDE.md")
    parser.add_argument("--manifest", default="project_docs/_manifest.json")
    parser.add_argument("--project-docs", default="project_docs")
    parser.add_argument("--apply", action="store_true", help="原子写入 CLAUDE.md")
    parser.add_argument("--json", action="store_true", help="stdout 仅输出 JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_stdio_utf8()
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest, docs_root=args.project_docs)
        result = update_claude_doc_index(args.claude, manifest, apply=args.apply)
        output = {
            "ok": True,
            "changed": result["changed"],
            "applied": bool(args.apply and result["changed"]),
            "path": result["path"],
        }
        if args.json:
            print(stable_json_dumps(output), end="")
        else:
            action = (
                "已更新"
                if output["applied"]
                else "需要更新" if output["changed"] else "无变化"
            )
            print(f"CLAUDE 文档树: {action}")
        return 0
    except (OSError, ClaudeIndexError, ProjectDocsContractError) as exc:
        if args.json:
            print(stable_json_dumps({"ok": False, "error": str(exc)}), end="")
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
