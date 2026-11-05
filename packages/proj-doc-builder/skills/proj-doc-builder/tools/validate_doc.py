# -*- coding: utf-8 -*-
"""Validate one generated ``project_docs`` document against its source directory.

The validator keeps the historical positional document argument and
``--source-dir``/``--strict``/``--json`` options, while adding explicit roots and
standard/placeholder profiles.  Structural (critical) failures always produce a
non-zero CLI status; ``--strict`` also makes editorial failures fatal.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Sequence
from urllib.parse import unquote

from project_docs_contract import (
    LinkDisposition,
    PathValidationError,
    ProjectDocsContractError,
    StrictUtf8Error,
    parse_markdown_links,
    read_utf8,
    stable_json_dumps,
    validate_manifest_relative_path,
)

EXIT_OK = 0
EXIT_VALIDATION_FAILED = 1
EXIT_CRITICAL_ERROR = 2
RESULT_SCHEMA_VERSION = 1

PROFILE_AUTO = "auto"
PROFILE_STANDARD = "standard"
PROFILE_PLACEHOLDER = "placeholder"
PROFILES = (PROFILE_AUTO, PROFILE_STANDARD, PROFILE_PLACEHOLDER)

BANNED_WORDS = [
    "工具文件",
    "辅助函数",
    "辅助功能",
    "杂项",
    "其他文件",
    "各种工具",
    "一些常量",
    "一些函数",
    "一些类",
    "杂项文件",
    "核心业务逻辑与基础设施功能",
    "规范化的接口抽象、数据处理、状态维护以及执行支持",
    "协同工作，确保整个生命周期的确定性、高可用与高一致性运行",
    "接受全流程自动化测试与验证",
]
STATUS_ICONS = ("✅", "🔶", "⬜")
# Kept for callers that imported this historical module-level constant.
PY_EXTENSIONS = {".py", ".pyx", ".pyi"}
EXCLUDED_SOURCE_DIRS = {
    ".git",
    ".svn",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".hypothesis",
    "node_modules",
    ".lingma",
    ".deepseek",
    ".vscode",
    ".idea",
    ".settings",
    ".dsh",
    "Debug",
    "test_reports",
    "_extract",
    "target",
    "project_docs",
    "sub_plan",
}
CRITICAL_CHECK_IDS = frozenset({1, 4, 5, 8, 9})

_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LINK_CELL_RE = re.compile(
    r"^\s*\[([^\]\r\n]*)\]\(\s*"
    r"(<[^>\r\n]*>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^()\r\n]*\)))?\s*\)\s*$"
)


@dataclass(frozen=True)
class _ValidationContext:
    doc_path: Path
    docs_root: Path
    doc_relative: str
    project_root: Path
    source_dir: Path | None
    source_files: tuple[str, ...] | None
    source_subdirs: tuple[str, ...] | None
    profile: str
    profile_mismatch: str | None = None


@dataclass(frozen=True)
class _TableRow:
    cells: tuple[str, ...]
    raw: str
    line_number: int


@dataclass(frozen=True)
class _ManifestRow:
    name: str
    row: _TableRow
    link_target: str | None


def _section(content: str, heading: str) -> str | None:
    """Extract a level-two Markdown section by its exact heading."""

    pattern = rf"^##[ \t]+{re.escape(heading)}[ \t]*\r?\n(.*?)(?=^##[ \t]+|\Z)"
    match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else None


def _section_any(content: str, headings: Sequence[str]) -> str | None:
    for heading in headings:
        section_text = _section(content, heading)
        if section_text is not None:
            return section_text
    return None


def _strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^[ \t]*(?:>|[-*+]\s+|\d+[.)]\s+)", "", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    return re.sub(r"\s+", " ", text).strip()


def _sentence_count(text: str) -> int:
    clean = _strip_markdown(text)
    if not clean:
        return 0
    fragments = re.split(r"(?:[。！？!?]+|(?<!\d)\.(?!\d)(?=\s|$|[）\)\"\'”’]))", clean)
    return sum(1 for fragment in fragments if re.search(r"[\w㐀-鿿]", fragment))


def _split_table_cells(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    if stripped.endswith("|"):
        stripped = stripped[1:-1]
    else:
        stripped = stripped[1:]
    cells = tuple(
        part.replace(r"\|", "|").strip() for part in re.split(r"(?<!\\)\|", stripped)
    )
    return cells if cells else None


def _unwrap_formatting(value: str) -> str:
    value = value.strip()
    changed = True
    while changed:
        changed = False
        for marker in ("**", "__"):
            if value.startswith(marker) and value.endswith(marker) and len(value) >= 4:
                value = value[len(marker) : -len(marker)].strip()
                changed = True
    if value.startswith("`") and value.endswith("`") and value.count("`") == 2:
        value = value[1:-1].strip()
    return value


def _cell_label_and_target(cell: str) -> tuple[str, str | None]:
    value = cell.strip()
    for marker in ("**", "__"):
        if value.startswith(marker) and value.endswith(marker):
            value = value[len(marker) : -len(marker)].strip()
    match = _LINK_CELL_RE.fullmatch(value)
    if match is not None:
        return _unwrap_formatting(match.group(1)), match.group(2)
    return _unwrap_formatting(value), None


def _table_rows(section_text: str, headers: set[str]) -> list[_TableRow]:
    rows: list[_TableRow] = []
    for line_number, line in enumerate(section_text.splitlines(), 1):
        cells = _split_table_cells(line)
        if not cells:
            continue
        if all(_TABLE_SEPARATOR_RE.fullmatch(cell.replace(" ", "")) for cell in cells):
            continue
        first_label, _ = _cell_label_and_target(cells[0])
        if first_label.strip() in headers:
            continue
        rows.append(_TableRow(cells=cells, raw=line, line_number=line_number))
    return rows


def _manifest_rows(
    section_text: str,
    *,
    kind: str,
    headers: set[str],
) -> tuple[list[_ManifestRow], list[str]]:
    manifest_rows: list[_ManifestRow] = []
    issues: list[str] = []
    for row in _table_rows(section_text, headers):
        label, target = _cell_label_and_target(row.cells[0])
        if kind == "file":
            if not label or label in {".", ".."} or "/" in label or "\\" in label:
                issues.append(f"表格第 {row.line_number} 行文件名无效: {label!r}")
                continue
            name = label
        else:
            if not label.endswith("/"):
                issues.append(
                    f"表格第 {row.line_number} 行子目录必须以 / 结尾: {label!r}"
                )
                continue
            name = label[:-1]
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                issues.append(f"表格第 {row.line_number} 行子目录名无效: {label!r}")
                continue
        manifest_rows.append(_ManifestRow(name=name, row=row, link_target=target))
    return manifest_rows, issues


def _is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_text(value: str | os.PathLike[str], label: str) -> str:
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise PathValidationError(f"{label}必须是文本路径")
    if not raw or "\0" in raw:
        raise PathValidationError(f"{label}为空或包含 NUL")
    return raw


def _resolve_directory(value: str | os.PathLike[str], label: str) -> Path:
    raw = _path_text(value, label)
    try:
        resolved = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(f"{label}不存在或不可解析: {raw}") from exc
    if not resolved.is_dir():
        raise PathValidationError(f"{label}不是目录: {raw}")
    return resolved


def _infer_docs_root(candidate: Path) -> Path:
    lexical = candidate if candidate.is_absolute() else Path.cwd() / candidate
    lexical = lexical.absolute()
    for parent in lexical.parents:
        if parent.name == "project_docs" and parent.is_dir():
            return parent.resolve(strict=True)

    default_root = Path.cwd() / "project_docs"
    if default_root.is_dir():
        resolved_default = default_root.resolve(strict=True)
        try:
            candidate.resolve(strict=False).relative_to(resolved_default)
            return resolved_default
        except (OSError, RuntimeError, ValueError):
            pass
    raise PathValidationError(
        "文档必须位于可识别的 project_docs 根目录内；请显式传入 --project-docs"
    )


def _candidate_under_root(raw: str, root: Path) -> Path:
    supplied = Path(raw)
    if supplied.is_absolute():
        return supplied

    cwd_candidate = Path.cwd() / supplied
    try:
        if _is_below(cwd_candidate.resolve(strict=False), root):
            return cwd_candidate
    except (OSError, RuntimeError):
        pass

    portable = raw.replace("\\", "/")
    pure = PurePosixPath(portable)
    if pure.parts and pure.parts[0] == root.name:
        pure = PurePosixPath(*pure.parts[1:])
    return root.joinpath(*pure.parts)


def _resolve_document(
    doc_path: str | os.PathLike[str],
    project_docs_dir: str | os.PathLike[str] | None,
) -> tuple[Path, Path, str]:
    raw = _path_text(doc_path, "文档路径")
    root = (
        _resolve_directory(project_docs_dir, "project_docs 根目录")
        if project_docs_dir is not None
        else _infer_docs_root(Path(raw))
    )
    candidate = _candidate_under_root(raw, root)
    try:
        lexical_resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(f"文档不可解析: {raw}") from exc
    if not _is_below(lexical_resolved, root):
        raise PathValidationError(f"文档路径越界: {raw}")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(f"文档不可解析: {raw}") from exc
    if not resolved.is_file():
        raise PathValidationError(f"文档不是普通文件: {raw}")
    if not _is_below(resolved, root):
        raise PathValidationError(f"文档路径越界: {raw}")
    relative = resolved.relative_to(root).as_posix()
    validate_manifest_relative_path(relative)
    return resolved, root, relative


def _resolve_source_directory(
    source_dir: str | os.PathLike[str],
    project_root: Path,
    docs_root: Path,
) -> Path:
    raw = _path_text(source_dir, "源目录")
    supplied = Path(raw)
    candidate = supplied if supplied.is_absolute() else Path.cwd() / supplied
    try:
        cwd_resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        cwd_resolved = None

    if supplied.is_absolute():
        try:
            lexical_resolved = supplied.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathValidationError(f"源目录不可解析: {raw}") from exc
        if not _is_below(lexical_resolved, project_root):
            raise PathValidationError(f"源目录越过项目根目录: {raw}")
    elif cwd_resolved is None or not _is_below(cwd_resolved, project_root):
        portable = PurePosixPath(raw.replace("\\", "/"))
        if portable.parts and portable.parts[0] == project_root.name:
            portable = PurePosixPath(*portable.parts[1:])
        candidate = project_root.joinpath(*portable.parts)

    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(f"源目录不存在或不可解析: {raw}") from exc
    if not resolved.is_dir():
        raise PathValidationError(f"源目录不是目录: {raw}")
    if not _is_below(resolved, project_root):
        raise PathValidationError(f"源目录越过项目根目录: {raw}")
    if _is_below(resolved, docs_root):
        raise PathValidationError(f"源目录不能位于 project_docs 内: {raw}")
    return resolved


def _source_inventory(
    source_dir: Path, project_root: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    files: list[str] = []
    subdirs: list[str] = []
    try:
        entries = sorted(source_dir.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise PathValidationError(f"无法读取源目录: {source_dir}") from exc

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        try:
            resolved = entry.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PathValidationError(f"源目录项不可解析: {entry}") from exc
        if not _is_below(resolved, project_root):
            raise PathValidationError(f"源目录项越过项目根目录: {entry}")
        if resolved.is_file():
            if source_dir == project_root and name.casefold() == "claude.md":
                continue
            files.append(name)
        elif resolved.is_dir() and name not in EXCLUDED_SOURCE_DIRS:
            subdirs.append(name)
    return tuple(files), tuple(subdirs)


def _content_is_placeholder(content: str) -> bool:
    status = _section(content, "状态") or ""
    return "⬜" in status or "占位目录" in content or "当前为占位" in content


def _source_is_placeholder(
    files: tuple[str, ...] | None,
    subdirs: tuple[str, ...] | None,
) -> bool | None:
    if files is None or subdirs is None:
        return None
    return not subdirs and set(files).issubset({"__init__.py"})


def _select_profile(
    requested: str,
    content: str,
    files: tuple[str, ...] | None,
    subdirs: tuple[str, ...] | None,
) -> tuple[str, str | None]:
    if requested not in PROFILES:
        raise ValueError(f"未知 profile: {requested!r}")
    source_placeholder = _source_is_placeholder(files, subdirs)
    mismatch: str | None = None

    if requested == PROFILE_AUTO:
        if source_placeholder is not None:
            profile = PROFILE_PLACEHOLDER if source_placeholder else PROFILE_STANDARD
        else:
            profile = (
                PROFILE_PLACEHOLDER
                if _content_is_placeholder(content)
                else PROFILE_STANDARD
            )
    else:
        profile = requested

    if profile == PROFILE_PLACEHOLDER and source_placeholder is False:
        mismatch = "placeholder profile 与含实质文件或子目录的源目录不一致"
    return profile, mismatch


def _build_context(
    doc_path: str | os.PathLike[str],
    source_dir: str | os.PathLike[str] | None,
    *,
    profile: str,
    project_docs_dir: str | os.PathLike[str] | None,
    project_root: str | os.PathLike[str] | None,
) -> tuple[_ValidationContext, str]:
    resolved_doc, docs_root, relative_doc = _resolve_document(
        doc_path, project_docs_dir
    )
    content = read_utf8(resolved_doc)

    resolved_project_root = (
        _resolve_directory(project_root, "项目根目录")
        if project_root is not None
        else docs_root.parent
    )
    if not _is_below(docs_root, resolved_project_root):
        raise PathValidationError("project_docs 根目录越过项目根目录")

    resolved_source: Path | None = None
    source_files: tuple[str, ...] | None = None
    source_subdirs: tuple[str, ...] | None = None
    if source_dir is not None:
        resolved_source = _resolve_source_directory(
            source_dir,
            resolved_project_root,
            docs_root,
        )
        source_files, source_subdirs = _source_inventory(
            resolved_source,
            resolved_project_root,
        )

    selected_profile, mismatch = _select_profile(
        profile,
        content,
        source_files,
        source_subdirs,
    )
    return (
        _ValidationContext(
            doc_path=resolved_doc,
            docs_root=docs_root,
            doc_relative=relative_doc,
            project_root=resolved_project_root,
            source_dir=resolved_source,
            source_files=source_files,
            source_subdirs=source_subdirs,
            profile=selected_profile,
            profile_mismatch=mismatch,
        ),
        content,
    )


def _legacy_inventory(
    source_dir: str | os.PathLike[str] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if source_dir is None:
        return None
    source = _resolve_directory(source_dir, "源目录")
    return _source_inventory(source, source)


def _inventory_for_check(
    source_dir: str | os.PathLike[str] | None,
    context: _ValidationContext | None,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    if context is not None:
        if context.source_files is None or context.source_subdirs is None:
            return None
        return context.source_files, context.source_subdirs
    return _legacy_inventory(source_dir)


def _duplicates(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)


def _decode_source_link_target(target: str, context: _ValidationContext) -> Path:
    cleaned = target.strip()
    if cleaned.startswith("<") or cleaned.endswith(">"):
        if not (cleaned.startswith("<") and cleaned.endswith(">")):
            raise PathValidationError("文件链接的尖括号不匹配")
        cleaned = cleaned[1:-1].strip()
    path_text = cleaned.partition("#")[0].partition("?")[0]
    if not path_text or _INVALID_PERCENT_RE.search(path_text):
        raise PathValidationError("文件链接为空或包含非法百分号转义")
    try:
        decoded = unquote(path_text, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PathValidationError("文件链接包含非 UTF-8 转义") from exc
    if (
        "\0" in decoded
        or "\\" in decoded
        or decoded.startswith("/")
        or decoded.startswith("//")
        or _URI_SCHEME_RE.match(decoded)
        or PureWindowsPath(decoded).drive
    ):
        raise PathValidationError("文件链接不是安全的项目内相对路径")
    try:
        resolved = context.doc_path.parent.joinpath(
            *PurePosixPath(decoded).parts
        ).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError("文件链接目标不存在或不可解析") from exc
    if not _is_below(resolved, context.project_root):
        raise PathValidationError("文件链接目标越过项目根目录")
    return resolved


def _validate_file_row_links(
    rows: Sequence[_ManifestRow],
    context: _ValidationContext,
) -> list[str]:
    if context.source_dir is None:
        return []
    issues: list[str] = []
    for item in rows:
        if item.link_target is None:
            continue
        try:
            actual_target = _decode_source_link_target(item.link_target, context)
            expected_target = (context.source_dir / item.name).resolve(strict=True)
        except (OSError, ProjectDocsContractError, ValueError) as exc:
            issues.append(f"文件 {item.name!r} 的链接无效: {exc}")
            continue
        if actual_target != expected_target:
            issues.append(f"文件 {item.name!r} 的链接未指向对应源码文件")
    return issues


def check_1_file_table(
    content: str,
    source_dir: str | os.PathLike[str] | None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check that the file manifest exactly matches immediate source files."""

    section_text = _section(content, "文件清单")
    if section_text is None:
        return False, "缺少 ## 文件清单 段"
    inventory = _inventory_for_check(source_dir, context)
    if inventory is None:
        return False, "未提供源目录，无法精确核验文件清单"

    rows, issues = _manifest_rows(
        section_text,
        kind="file",
        headers={"文件", "文件名"},
    )
    actual_files = set(inventory[0])
    listed_names = [row.name for row in rows]
    duplicate_names = _duplicates(listed_names)
    missing = sorted(actual_files - set(listed_names))
    extra = sorted(set(listed_names) - actual_files)
    if duplicate_names:
        issues.append(f"重复文件: {', '.join(duplicate_names)}")
    if missing:
        issues.append(f"遗漏文件: {', '.join(missing)}")
    if extra:
        issues.append(f"多余文件: {', '.join(extra)}")
    if context is not None:
        issues.extend(_validate_file_row_links(rows, context))
    if issues:
        return False, "; ".join(issues)
    return True, f"文件清单与源目录精确一致（{len(actual_files)} 个文件）"


def check_2_overview(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check profile-specific overview requirements."""

    del source_dir
    section_text = _section(content, "概述")
    if not section_text:
        return False, "缺少 ## 概述 段"
    profile = context.profile if context is not None else PROFILE_STANDARD
    sentences = _sentence_count(section_text)
    if profile == PROFILE_PLACEHOLDER:
        if sentences < 1 or "占位" not in _strip_markdown(section_text):
            return False, "placeholder 概述需明确说明占位状态"
        return True, f"placeholder 概述 {sentences} 句话"
    if not 3 <= sentences <= 4:
        return False, f"标准概述为 {sentences} 句话（需 3–4 句）"
    return True, f"标准概述 {sentences} 句话"


def _valid_placeholder_role_link(
    section_text: str,
    context: _ValidationContext | None,
) -> tuple[bool, str | None]:
    if context is None:
        return False, None
    links = parse_markdown_links(
        section_text,
        source_doc=context.doc_path,
        docs_root=context.docs_root,
    )
    if not links:
        return False, None
    for link in links:
        if link.disposition is LinkDisposition.INVALID:
            return False, f"系统角色链接不安全: {link.raw_target}"
        if link.disposition is LinkDisposition.LOCAL_DOCUMENT and link.exists is False:
            return False, f"系统角色链接不存在: {link.raw_target}"
    return (
        any(
            link.disposition is LinkDisposition.LOCAL_DOCUMENT and link.exists
            for link in links
        ),
        None,
    )


def check_3_system_role(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check that system-role prose is substantive for its profile."""

    del source_dir
    section_text = _section(content, "系统角色")
    if not section_text:
        return False, "缺少 ## 系统角色 段"
    clean = _strip_markdown(section_text)
    profile = context.profile if context is not None else PROFILE_STANDARD
    if profile == PROFILE_PLACEHOLDER:
        has_valid_link, link_error = _valid_placeholder_role_link(section_text, context)
        if link_error:
            return False, link_error
        if has_valid_link or len(clean) >= 8:
            return True, "placeholder 系统角色已提供简要定位或父文档链接"
        return False, "placeholder 系统角色为空或缺少有效父文档链接"

    if len(clean) < 50:
        return False, f"系统角色过于简短（{len(clean)} 字符，需 >= 50）"
    architecture_terms = (
        "子系统",
        "协议栈",
        "模块",
        "组件",
        "算法",
        "固件",
        "PC 端",
        "测试层",
        "数据层",
        "接口层",
    )
    relation_terms = (
        "上游",
        "下游",
        "依赖",
        "调用",
        "协作",
        "输入",
        "输出",
        "数据流",
        "提供",
        "使用",
        "传递",
        "导出",
        "被",
    )
    if not any(term in clean for term in architecture_terms):
        return False, "系统角色未说明所属子系统、协议层或模块"
    if not any(term in clean for term in relation_terms):
        return False, "系统角色未说明输入输出、依赖或上下游协作关系"
    return True, f"系统角色包含实质架构定位与协作关系（{len(clean)} 字符）"


def _expected_child_doc(context: _ValidationContext, child_name: str) -> str:
    if context.doc_relative in ("index.md", ""):
        return f"{child_name}.md"
    current_without_suffix = context.doc_relative[: -len(".md")]
    return PurePosixPath(current_without_suffix, f"{child_name}.md").as_posix()


def _validate_subdir_links(
    rows: Sequence[_ManifestRow],
    context: _ValidationContext,
) -> list[str]:
    issues: list[str] = []
    for item in rows:
        links = parse_markdown_links(
            item.row.raw,
            source_doc=context.doc_path,
            docs_root=context.docs_root,
        )
        if len(links) != 1:
            issues.append(f"子目录 {item.name!r} 必须且只能有一个 Markdown 文档链接")
            continue
        link = links[0]
        expected = _expected_child_doc(context, item.name)
        if link.disposition is LinkDisposition.INVALID:
            issues.append(f"子目录 {item.name!r} 链接不安全: {link.reason}")
        elif link.disposition is not LinkDisposition.LOCAL_DOCUMENT:
            issues.append(f"子目录 {item.name!r} 未链接本地 Markdown 文档")
        elif link.project_relative_path != expected:
            issues.append(
                f"子目录 {item.name!r} 链接目标错误: "
                f"{link.project_relative_path!r}，应为 {expected!r}"
            )
        elif link.exists is not True:
            issues.append(f"子目录 {item.name!r} 文档不存在: {expected}")
    return issues


def check_4_subdir_table(
    content: str,
    source_dir: str | os.PathLike[str] | None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check source subdirectories and their exact Markdown document links."""

    inventory = _inventory_for_check(source_dir, context)
    if inventory is None:
        return False, "未提供源目录，无法精确核验子目录清单"
    actual_subdirs = set(inventory[1])
    section_text = _section_any(content, ("子目录", "子目录清单"))
    if section_text is None:
        if actual_subdirs:
            return False, f"有 {len(actual_subdirs)} 个子目录但缺少 ## 子目录 段"
        return True, "N/A（源目录无子目录）"

    rows, issues = _manifest_rows(
        section_text,
        kind="subdir",
        headers={"子目录", "目录"},
    )
    listed_names = [row.name for row in rows]
    duplicate_names = _duplicates(listed_names)
    missing = sorted(actual_subdirs - set(listed_names))
    extra = sorted(set(listed_names) - actual_subdirs)
    if duplicate_names:
        issues.append(f"重复子目录: {', '.join(duplicate_names)}")
    if missing:
        issues.append(f"遗漏子目录: {', '.join(missing)}")
    if extra:
        issues.append(f"多余子目录: {', '.join(extra)}")
    if context is not None:
        issues.extend(_validate_subdir_links(rows, context))
    elif actual_subdirs:
        issues.append("缺少文档上下文，无法精确核验子目录 Markdown 链接")
    if issues:
        return False, "; ".join(issues)
    if not actual_subdirs:
        return True, "N/A（源目录无子目录）"
    return True, f"子目录清单及链接精确一致（{len(actual_subdirs)} 个子目录）"


def check_5_status(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check status marker and profile consistency."""

    del source_dir
    section_text = _section(content, "状态")
    if not section_text:
        return False, "缺少 ## 状态 段"
    if context is not None and context.profile_mismatch:
        return False, context.profile_mismatch
    icons = [icon for icon in STATUS_ICONS if icon in section_text]
    if len(icons) != 1:
        return False, "状态段必须且只能包含一个有效图标（✅/🔶/⬜）"
    profile = context.profile if context is not None else PROFILE_STANDARD
    icon = icons[0]
    if profile == PROFILE_PLACEHOLDER and icon != "⬜":
        return False, "placeholder profile 必须使用 ⬜ 占位状态"
    if profile == PROFILE_STANDARD and icon == "⬜":
        return False, "standard profile 不能使用 ⬜ 占位状态"
    return True, f"状态与 {profile} profile 一致: {icon}"


def _extract_ast_symbols(source_file: Path) -> set[str]:
    try:
        source_code = source_file.read_text(encoding="utf-8")
        tree = ast.parse(source_code, filename=str(source_file))
    except Exception:
        return set()
    symbols: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                symbols.add(node.target.id)
    return symbols


def check_6_symbol_table(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check key symbols and verify their AST presence in source files."""

    profile = context.profile if context is not None else PROFILE_STANDARD
    if profile == PROFILE_PLACEHOLDER:
        return True, "N/A（placeholder 不强制关键符号）"

    inventory = _inventory_for_check(source_dir, context)
    is_python_package = inventory is not None and "__init__.py" in inventory[0]
    if not is_python_package:
        return True, "N/A（非 Python 包）"
    section_text = _section(content, "关键符号")
    if not section_text:
        return False, "Python 包目录缺少 ## 关键符号 段"
    rows = _table_rows(section_text, {"符号", "名称"})
    symbol_names: list[str] = []
    issues: list[str] = []

    for row in rows:
        label, _ = _cell_label_and_target(row.cells[0])
        if not label:
            continue
        symbol_names.append(label)

        # AST-level reality verification when source files are present
        if (
            context is not None
            and context.source_dir is not None
            and len(row.cells) >= 3
        ):
            loc = _unwrap_formatting(row.cells[2])
            file_part = loc.split(":")[0].strip()
            if file_part.endswith(".py"):
                source_file = context.source_dir / file_part
                if source_file.is_file():
                    ast_symbols = _extract_ast_symbols(source_file)
                    clean_name = label.split("(")[0].strip()
                    root_name = clean_name.split(".")[0].strip()
                    if (
                        ast_symbols
                        and clean_name not in ast_symbols
                        and root_name not in ast_symbols
                    ):
                        issues.append(f"符号 {label!r} 未在 {file_part} 的 AST 定义中找到")
                else:
                    issues.append(f"关键符号定义位置文件 {file_part!r} 在源目录中不存在")

    if issues:
        return False, "; ".join(issues)
    if len(symbol_names) < 3:
        return False, f"Python 包关键符号仅 {len(symbol_names)} 个（需 >= 3）"
    return True, f"关键符号 {len(symbol_names)} 个"


def check_7_no_banned_words(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check that vague banned terms are absent."""

    del source_dir, context
    found: list[str] = []
    lines = content.splitlines()
    for word in BANNED_WORDS:
        for line_number, line in enumerate(lines, 1):
            if word in line:
                found.append(f"'{word}' (L{line_number})")
                break
    if found:
        return False, f"发现禁止词: {', '.join(found)}"
    return True, "无禁止词"


def check_8_breadcrumb(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check breadcrumb navigation before the first level-two section."""

    del source_dir, context
    intro = re.split(r"^##[ \t]+", content, maxsplit=1, flags=re.MULTILINE)[0]
    if re.search(r"^\s*\*\*导航\*\*\s*:", intro, re.MULTILINE):
        return True, "面包屑导航存在"
    if re.search(r"^\s*>?\s*面包屑\s*:", intro, re.MULTILINE):
        return True, "面包屑导航存在（兼容旧格式）"
    return False, "缺少文档开头的面包屑导航 (**导航**: ...)"


def _dependency_row_issue(
    row: _TableRow, context: _ValidationContext | None
) -> str | None:
    if len(row.cells) < 3:
        return f"依赖关系表第 {row.line_number} 行列数不足"
    direction = _strip_markdown(row.cells[0])
    if not re.match(r"^(?:⬆|⬇|↔)", direction):
        return f"依赖关系表第 {row.line_number} 行缺少方向标记"
    module = _strip_markdown(row.cells[1])
    relationship = _strip_markdown(row.cells[2])
    empty_values = {"", "-", "—", "无", "N/A", "n/a", "TODO", "待补充"}
    if module in empty_values:
        return f"依赖关系表第 {row.line_number} 行模块名无实质内容"
    if relationship in empty_values or len(relationship) < 6:
        return f"依赖关系表第 {row.line_number} 行关系说明不够具体"

    if context is None:
        if not re.search(r"\[[^\]]+\]\([^)]+\)", row.raw):
            return f"依赖关系表第 {row.line_number} 行缺少 Markdown 文档链接"
        return None

    links = parse_markdown_links(
        row.raw,
        source_doc=context.doc_path,
        docs_root=context.docs_root,
    )
    invalid = [link for link in links if link.disposition is LinkDisposition.INVALID]
    if invalid:
        return f"依赖关系表第 {row.line_number} 行包含不安全链接: {invalid[0].reason}"
    local_links = [
        link for link in links if link.disposition is LinkDisposition.LOCAL_DOCUMENT
    ]
    if len(local_links) != 1:
        return f"依赖关系表第 {row.line_number} 行必须且只能有一个本地文档链接"
    link = local_links[0]
    if link.exists is not True:
        return f"依赖关系表第 {row.line_number} 行链接目标不存在: {link.raw_target}"
    if link.project_relative_path == context.doc_relative:
        return f"依赖关系表第 {row.line_number} 行不能仅链接当前文档"
    return None


def check_9_dependency_table(
    content: str,
    source_dir: str | os.PathLike[str] | None = None,
    *,
    context: _ValidationContext | None = None,
) -> tuple[bool, str]:
    """Check a substantive dependency table and its safe local document links."""

    del source_dir
    profile = context.profile if context is not None else PROFILE_STANDARD
    if profile == PROFILE_PLACEHOLDER:
        return True, "N/A（placeholder 不强制依赖关系表）"
    section_text = _section(content, "依赖关系")
    if not section_text:
        return False, "缺少 ## 依赖关系 段"
    rows = _table_rows(section_text, {"方向"})
    if not rows:
        return False, "依赖关系表为空或不是 Markdown 表格"
    issues = [
        issue
        for row in rows
        if (issue := _dependency_row_issue(row, context)) is not None
    ]
    if issues:
        return False, "; ".join(issues)
    return True, f"依赖关系 {len(rows)} 条，说明及链接均有效"


CHECKS = [
    (1, "file_manifest_table", "文件清单表格", check_1_file_table),
    (2, "overview_min_3", "概述 3–4 句", check_2_overview),
    (3, "system_role", "系统角色段", check_3_system_role),
    (4, "subdir_table", "子目录表", check_4_subdir_table),
    (5, "status_marker", "状态标记", check_5_status),
    (6, "symbol_table", "关键符号表", check_6_symbol_table),
    (7, "no_banned_words", "无禁止词", check_7_no_banned_words),
    (8, "breadcrumb", "面包屑导航", check_8_breadcrumb),
    (9, "dependency_table", "依赖关系表", check_9_dependency_table),
]


def _display_path(value: str | os.PathLike[str] | None) -> str | None:
    if value is None:
        return None
    try:
        return os.fspath(value)
    except TypeError:
        return repr(value)


def _not_run_checks(reason: str) -> list[dict[str, Any]]:
    return [
        {
            "id": check_id,
            "name": name,
            "label": label,
            "severity": "critical" if check_id in CRITICAL_CHECK_IDS else "advisory",
            "result": "N/A",
            "detail": f"未执行: {reason}",
        }
        for check_id, name, label, _ in CHECKS
    ]


def _critical_result(
    doc_path: str | os.PathLike[str],
    source_dir: str | os.PathLike[str] | None,
    requested_profile: str,
    *,
    error_code: str,
    message: str,
) -> dict[str, Any]:
    profile = (
        requested_profile
        if requested_profile in {PROFILE_STANDARD, PROFILE_PLACEHOLDER}
        else None
    )
    checks = _not_run_checks(message)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "file": _display_path(doc_path),
        "source_dir": _display_path(source_dir),
        "requested_profile": requested_profile,
        "profile": profile,
        "checks": checks,
        "passed": 0,
        "failed": 0,
        "na": len(checks),
        "critical_failed": 1,
        "critical": True,
        "qualified": False,
        "ok": False,
        "error_code": error_code,
        "error": message,
        "errors": [message],
    }


def _invalid_link_errors(content: str, context: _ValidationContext) -> list[str]:
    errors: list[str] = []
    for link in parse_markdown_links(
        content,
        source_doc=context.doc_path,
        docs_root=context.docs_root,
    ):
        if link.disposition is LinkDisposition.INVALID:
            errors.append(f"不安全 Markdown 链接 {link.raw_target!r}: {link.reason}")
    return sorted(set(errors))


def validate_doc(
    doc_path: str | os.PathLike[str],
    source_dir: str | os.PathLike[str] | None = None,
    *,
    profile: str = PROFILE_AUTO,
    project_docs_dir: str | os.PathLike[str] | None = None,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Run all checks and always return the same complete result structure."""

    try:
        context, content = _build_context(
            doc_path,
            source_dir,
            profile=profile,
            project_docs_dir=project_docs_dir,
            project_root=project_root,
        )
    except FileNotFoundError:
        return _critical_result(
            doc_path,
            source_dir,
            profile,
            error_code="document-not-found",
            message="file not found",
        )
    except StrictUtf8Error as exc:
        return _critical_result(
            doc_path,
            source_dir,
            profile,
            error_code="invalid-utf8",
            message=str(exc),
        )
    except (OSError, ValueError, ProjectDocsContractError) as exc:
        return _critical_result(
            doc_path,
            source_dir,
            profile,
            error_code="path-or-input-error",
            message=str(exc),
        )

    critical_errors = _invalid_link_errors(content, context)
    results: list[dict[str, Any]] = []
    for check_id, name, label, function in CHECKS:
        try:
            passed, detail = function(content, source_dir, context=context)
        except (
            Exception
        ) as exc:  # A broken check must be reported, never hide the rest.
            passed = False
            detail = f"检查异常: {type(exc).__name__}: {exc}"
        status = (
            "N/A"
            if passed and detail.startswith("N/A")
            else ("PASS" if passed else "FAIL")
        )
        results.append(
            {
                "id": check_id,
                "name": name,
                "label": label,
                "severity": (
                    "critical" if check_id in CRITICAL_CHECK_IDS else "advisory"
                ),
                "result": status,
                "detail": detail,
            }
        )

    passed_count = sum(result["result"] == "PASS" for result in results)
    failed_count = sum(result["result"] == "FAIL" for result in results)
    na_count = sum(result["result"] == "N/A" for result in results)
    critical_check_failures = sum(
        result["result"] == "FAIL" and result["severity"] == "critical"
        for result in results
    )
    critical_failed = critical_check_failures + len(critical_errors)
    qualified = failed_count == 0 and not critical_errors

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "file": _display_path(doc_path),
        "source_dir": _display_path(source_dir),
        "requested_profile": profile,
        "profile": context.profile,
        "checks": results,
        "passed": passed_count,
        "failed": failed_count,
        "na": na_count,
        "critical_failed": critical_failed,
        "critical": critical_failed > 0,
        "qualified": qualified,
        "ok": qualified,
        "error_code": "unsafe-markdown-link" if critical_errors else None,
        "error": critical_errors[0] if critical_errors else None,
        "errors": critical_errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a project_docs document against its source directory"
    )
    parser.add_argument("doc", help="Path to the .md document to validate")
    parser.add_argument(
        "--source-dir",
        "--source_dir",
        "-s",
        dest="source_dir",
        help="Corresponding source directory for exact inventory comparison",
    )
    parser.add_argument(
        "--project-docs",
        "--docs-root",
        dest="project_docs",
        help="Trusted project_docs root (otherwise inferred from the document path)",
    )
    parser.add_argument(
        "--project-root",
        help="Trusted project root used to bound the source directory",
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=PROFILE_AUTO,
        help="Validation profile (default: auto)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero for advisory as well as critical check failures",
    )
    parser.add_argument("--json", action="store_true", help="Write only JSON to stdout")
    return parser


def _result_exit_code(result: dict[str, Any], *, strict: bool) -> int:
    if result["error_code"] is not None:
        return EXIT_CRITICAL_ERROR
    if result["critical_failed"]:
        return EXIT_VALIDATION_FAILED
    if strict and result["failed"]:
        return EXIT_VALIDATION_FAILED
    return EXIT_OK


def _configure_stdout_utf8() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_stdout_utf8()
    result = validate_doc(
        args.doc,
        args.source_dir,
        profile=args.profile,
        project_docs_dir=args.project_docs,
        project_root=args.project_root,
    )
    exit_code = _result_exit_code(result, strict=args.strict)

    if args.json:
        print(stable_json_dumps(result), end="")
        return exit_code

    print(f"=== {result['file']} ===")
    if result["error"]:
        print(f"ERROR [{result['error_code']}]: {result['error']}", file=sys.stderr)
    for check in result["checks"]:
        icon = {"PASS": "✅", "FAIL": "❌", "N/A": "⬜"}[check["result"]]
        print(f"  {icon} #{check['id']} {check['label']}: {check['detail']}")
    print("  ---")
    print(
        f"  PASS: {result['passed']}  FAIL: {result['failed']}  "
        f"N/A: {result['na']}  CRITICAL: {result['critical_failed']}"
    )
    print(f"  Profile: {result['profile']}  Qualified: {result['qualified']}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
