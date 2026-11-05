# -*- coding: utf-8 -*-
"""Verify and load generated ``project_docs`` documentation for AI consumption.

Two modes, both **verify before serving**:

``overview``
    Verified manifest metadata + rendered document tree + per-document
    title/status/bytes and (optionally) its section names. Small by default so a
    fresh agent conversation can load the shape of a whole project cheaply.

``target``
    One verified document — the whole file, or a single ``##`` section.

This tool is a *reader*: it never writes. It reuses
``project_docs_contract`` so readers and writers share one set of path, link,
manifest and hashing rules instead of drifting apart.

Exit codes:
    0  loaded and verified
    1  the manifest does not match the documents (stale index), or the requested
       target/section is not registered
    2  usage, path-safety or encoding error
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import claude_doc_index
import project_docs_contract as contract

EXIT_OK = 0
EXIT_STALE_OR_MISSING = 1
EXIT_INPUT_ERROR = 2

SCHEMA_VERSION = 1
DOCS_DIRNAME = "project_docs"
_SECTION_RE = re.compile(r"^##[ \t]+(?P<title>\S.*?)[ \t]*$", re.MULTILINE)


def _docs_root(project_root: str | Path) -> Path:
    """Accept either the project root or the ``project_docs`` directory itself."""

    root = Path(project_root)
    candidate = root / DOCS_DIRNAME if (root / DOCS_DIRNAME).is_dir() else root
    return contract.resolve_docs_root(candidate)


def _text_result(mode: str, code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "mode": mode,
        "verified": False,
        "errors": [{"code": code, "message": message}],
    }


def _load_verified_manifest(docs_root: Path, mode: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    """Return ``(manifest, error_result, exit_code)``; exactly one is non-None."""

    try:
        manifest = contract.load_manifest(
            docs_root,
            verify_files=True,
            verify_complete_set=True,
        )
    except FileNotFoundError:
        return None, _text_result(
            mode,
            "manifest-missing",
            f"no {contract.MANIFEST_FILENAME} in {docs_root}; run the synchronizer first",
        ), EXIT_STALE_OR_MISSING
    except (
        contract.ManifestFileMismatchError,
        contract.ManifestValidationError,
        contract.ManifestSchemaError,
    ) as exc:
        return None, _text_result(
            mode,
            "manifest-stale",
            "manifest does not match the documents on disk, refusing to serve a "
            f"stale index ({exc})",
        ), EXIT_STALE_OR_MISSING
    except contract.ProjectDocsContractError as exc:
        return None, _text_result(mode, "manifest-invalid", str(exc)), EXIT_STALE_OR_MISSING
    except (OSError, ValueError) as exc:
        return None, _text_result(mode, "manifest-unreadable", str(exc)), EXIT_STALE_OR_MISSING
    return manifest, None, EXIT_OK


def split_sections(content: str) -> list[dict[str, str]]:
    """Split Markdown into ``##`` sections, preserving order and body text."""

    matches = list(_SECTION_RE.finditer(content))
    sections: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group("title").strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections.append({"title": title, "body": content[start:end].strip()})
    return sections


def _normalize_target(value: str) -> str:
    """Map a source directory, a document path, or a project-relative path to a doc path."""

    raw = value.strip().replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith(f"{DOCS_DIRNAME}/"):
        raw = raw[len(DOCS_DIRNAME) + 1 :]
    if raw in {"", ".", "/"}:
        raw = "index.md"
    elif not raw.endswith(".md"):
        raw = raw.rstrip("/") + ".md"
    return contract.validate_manifest_relative_path(raw).as_posix()


def _entry_by_path(manifest: Mapping[str, Any], relative: str) -> dict[str, Any] | None:
    entries = manifest.get("files", [])
    folded = relative.casefold()
    for entry in entries:
        if isinstance(entry, Mapping) and str(entry.get("path", "")).casefold() == folded:
            return dict(entry)
    return None


def overview(
    manifest: Mapping[str, Any],
    docs_root: Path,
    *,
    limit: int = 80,
    sections: int = 6,
    include_tree: bool = True,
) -> dict[str, Any]:
    """Verified project shape: tree, counts, and per-document metadata."""

    entries = [dict(item) for item in manifest.get("files", []) if isinstance(item, Mapping)]
    entries.sort(key=lambda item: str(item.get("path", "")))
    returned = entries if limit <= 0 else entries[:limit]

    documents: list[dict[str, Any]] = []
    for entry in returned:
        relative = str(entry.get("path", ""))
        item = {
            "path": relative,
            "title": entry.get("title"),
            "status": entry.get("status"),
            "bytes": entry.get("bytes"),
            "depth": entry.get("depth"),
        }
        if sections > 0:
            try:
                content = contract.read_project_document(docs_root, relative)
            except contract.ProjectDocsContractError:
                content = ""
            item["sections"] = [item_["title"] for item_ in split_sections(content)][:sections]
        documents.append(item)

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "mode": "overview",
        "verified": True,
        "docs_root": DOCS_DIRNAME,
        "manifest": {
            "schema_version": manifest.get("schema_version"),
            "generation_id": manifest.get("generation_id"),
            "last_synced": manifest.get("last_synced"),
            "total": manifest.get("total"),
            "depth_counts": manifest.get("depth_counts"),
        },
        "counts": {
            "documents": len(entries),
            "returned": len(documents),
        },
        "documents": documents,
    }
    if include_tree:
        result["tree"] = claude_doc_index.render_document_tree(manifest)
    if len(documents) < len(entries):
        result["omitted"] = {
            "documents": len(entries) - len(documents),
            "reason": f"limit={limit}; pass --limit 0 for every document",
        }
    return result


def target(
    manifest: Mapping[str, Any],
    docs_root: Path,
    requested: str,
    *,
    section: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Load one verified document, optionally narrowed to a single ``##`` section."""

    try:
        relative = _normalize_target(requested)
    except (contract.PathValidationError, ValueError) as exc:
        return _text_result("target", "unsafe-target", str(exc)), EXIT_INPUT_ERROR

    entry = _entry_by_path(manifest, relative)
    if entry is None:
        return _text_result(
            "target",
            "target-not-registered",
            f"{relative} is not registered in the manifest; "
            "run --dry-run to see the current worklist",
        ), EXIT_STALE_OR_MISSING

    canonical = str(entry["path"])
    try:
        content = contract.read_project_document(docs_root, canonical)
    except contract.ProjectDocsContractError as exc:
        return _text_result("target", "target-unreadable", str(exc)), EXIT_INPUT_ERROR

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "mode": "target",
        "verified": True,
        "doc": {
            "path": canonical,
            "title": entry.get("title"),
            "status": entry.get("status"),
            "bytes": entry.get("bytes"),
            "sha256": entry.get("sha256"),
            "synced_at": entry.get("synced_at"),
        },
        "available_sections": [item["title"] for item in split_sections(content)],
    }

    if section is None:
        result["content"] = content
        return result, EXIT_OK

    wanted = section.strip().casefold()
    for item in split_sections(content):
        if item["title"].casefold() == wanted:
            result["section"] = item["title"]
            result["content"] = item["body"]
            return result, EXIT_OK
    failure = _text_result(
        "target",
        "section-not-found",
        f"no '## {section}' section in {canonical}",
    )
    failure["available_sections"] = result["available_sections"]
    return failure, EXIT_STALE_OR_MISSING


def execute(
    project_root: str | Path,
    *,
    mode: str = "overview",
    requested: str | None = None,
    section: str | None = None,
    limit: int = 80,
    sections: int = 6,
    include_tree: bool = True,
) -> tuple[dict[str, Any], int]:
    """Run one load: verify the manifest, then serve the requested view."""

    try:
        docs_root = _docs_root(project_root)
    except contract.ProjectDocsContractError as exc:
        return _text_result(mode, "docs-root-invalid", str(exc)), EXIT_INPUT_ERROR
    except OSError as exc:
        return _text_result(mode, "project-root-invalid", str(exc)), EXIT_INPUT_ERROR

    manifest, error, exit_code = _load_verified_manifest(docs_root, mode)
    if manifest is None:
        assert error is not None
        return error, exit_code

    if mode == "overview":
        return overview(
            manifest,
            docs_root,
            limit=limit,
            sections=sections,
            include_tree=include_tree,
        ), EXIT_OK
    if mode == "target":
        if requested is None:
            return _text_result(mode, "target-required", "--target is required for target mode"), EXIT_INPUT_ERROR
        return target(manifest, docs_root, requested, section=section)
    return _text_result(mode, "unknown-mode", f"unsupported mode {mode!r}"), EXIT_INPUT_ERROR


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify and load generated project_docs documentation",
    )
    parser.add_argument("root", nargs="?", default=".", help="project root or project_docs")
    parser.add_argument(
        "--mode",
        choices=("overview", "target"),
        default="overview",
        help="overview = project shape; target = one document",
    )
    parser.add_argument(
        "--target",
        "-t",
        default=None,
        help="source directory, document path, or project-relative path",
    )
    parser.add_argument(
        "--section",
        "-s",
        default=None,
        help="with --target: return only this '##' section",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=80,
        help="max documents in overview (0 = all)",
    )
    parser.add_argument(
        "--sections",
        type=int,
        default=6,
        help="max section names per document in overview (0 = none)",
    )
    parser.add_argument("--no-tree", action="store_true", help="omit the rendered tree")
    parser.add_argument("--json", action="store_true", help="emit exactly one JSON document")
    return parser


def _emit(result: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(contract.stable_json_dumps(dict(result)))
        return
    if not result.get("ok"):
        for error in result.get("errors", []):
            print(f"ERROR [{error['code']}]: {error['message']}", file=sys.stderr)
        return
    if result.get("mode") == "overview":
        manifest = result["manifest"]
        print(
            f"documents: {result['counts']['documents']} "
            f"(returned {result['counts']['returned']}) "
            f"generation: {manifest['generation_id']}"
        )
        print(result.get("tree", ""))
        for item in result["documents"]:
            sections = ", ".join(item.get("sections") or [])
            print(f"- {item['path']} — {item['title']} {item['status']}")
            if sections:
                print(f"    sections: {sections}")
        return
    print(result.get("content", ""))


def main(argv: Sequence[str] | None = None) -> int:
    contract.configure_stdio_utf8()
    args = _parser().parse_args(argv)
    result, exit_code = execute(
        args.root,
        mode=args.mode,
        requested=args.target,
        section=args.section,
        limit=args.limit,
        sections=args.sections,
        include_tree=not args.no_tree,
    )
    result["exit_code"] = exit_code
    _emit(result, as_json=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
