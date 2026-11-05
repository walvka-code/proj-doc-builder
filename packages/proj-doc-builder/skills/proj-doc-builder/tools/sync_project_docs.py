# -*- coding: utf-8 -*-
"""Deterministic coordinator for the ``proj-doc-builder`` publish transaction.

The coordinator does not generate prose.  It stages the current document tree,
mechanically normalizes only known marker corruption, runs all safety gates, and
publishes marker/CLAUDE/manifest/scan-state changes in a defined order.  Dry-run
is the default; project files are changed only when ``--apply`` is explicit.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import os
import posixpath
import shutil
import stat
import sys
import tempfile
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

# Standalone CLI execution must not create project-local bytecode while loading
# sibling tools.  This must be set before the first sibling import below.
sys.dont_write_bytecode = True

import claude_doc_index
import link_check
import normalize_markers
import phase4_exit
import project_docs_contract as contract
import scan_dirs
import validate_doc

RESULT_SCHEMA_VERSION = 1
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_INPUT_ERROR = 2
EXIT_PUBLISH_FAILED = 3
SCAN_STATE_FILENAME = "_scan_state.json"
PUBLISH_JOURNAL_SCHEMA_VERSION = 2
PUBLISH_JOURNAL_KIND = "proj-doc-builder-publish-transaction"
_PUBLISH_JOURNAL_TEMP_MARKER = contract.COORDINATOR_TEMP_MARKER
_PUBLISH_ARTIFACT_ROOT_MARKER = contract.COORDINATOR_ARTIFACT_ROOT_MARKER
_PUBLISH_RETAINED_ARTIFACT_MARKER = contract.COORDINATOR_RETAINED_ARTIFACT_MARKER
_PUBLISH_HISTORY_MARKER = contract.COORDINATOR_HISTORY_MARKER


class SyncCoordinatorError(RuntimeError):
    """Base class for stable coordinator failures."""


class SyncInputError(SyncCoordinatorError):
    """The project or persisted input is unsafe, malformed, or incomplete."""


class PublishDisposition(str, Enum):
    """Mutually exclusive durable disposition for a publication unit."""

    NOT_ATTEMPTED = "not-attempted"
    UNCHANGED = "unchanged"
    INSTALLED = "installed"
    NOT_INSTALLED = "not-installed"
    PARTIALLY_INSTALLED = "partially-installed"
    UNKNOWN = "unknown"


_PARTIAL_DISPOSITIONS = {
    PublishDisposition.PARTIALLY_INSTALLED,
    PublishDisposition.UNKNOWN,
}


def _set_component_disposition(
    publish: dict[str, Any],
    component: str,
    disposition: PublishDisposition,
) -> None:
    """Set one component verdict and derive its legacy compatibility flags."""

    if component not in {"manifest", "scan_state"}:
        raise ValueError(f"unsupported publish component: {component}")
    publish[f"{component}_disposition"] = disposition.value
    if component == "manifest":
        publish["manifest_commit_point"] = disposition is PublishDisposition.INSTALLED
        publish["manifest_commit_ambiguous"] = disposition is PublishDisposition.UNKNOWN
    else:
        publish["scan_state_committed"] = disposition is PublishDisposition.INSTALLED
        publish["scan_state_commit_ambiguous"] = (
            disposition is PublishDisposition.UNKNOWN
        )


def _set_transaction_disposition(
    publish: dict[str, Any],
    disposition: PublishDisposition,
) -> None:
    """Set the transaction verdict and derive the legacy partial flag."""

    publish["disposition"] = disposition.value
    publish["partial_commit"] = disposition in _PARTIAL_DISPOSITIONS


class SyncPublishError(SyncCoordinatorError):
    """The publish transaction failed; ``result`` describes commit state."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    docs_root: Path
    claude_path: Path
    manifest_path: Path
    scan_state_path: Path
    root_identity: tuple[int, int]
    docs_root_identity: tuple[int, int]


@dataclass
class PreparedSync:
    paths: ProjectPaths
    stage_docs: Path
    run_timestamp: str
    force_full: bool
    original_documents: dict[str, str]
    marker_contents: dict[str, str]
    marker_details: list[dict[str, Any]]
    original_claude: str
    original_manifest_bytes: bytes | None
    original_scan_state_bytes: bytes | None
    previous_scan_state: dict[str, Any] | None
    scan_candidate: dict[str, Any]
    scan_commit_needed: bool
    manifest: dict[str, Any]
    manifest_changed: bool
    current_manifest_generation: str | None
    current_manifest_schema: int | None
    claude_candidate: str
    claude_changed: bool
    validation: dict[str, Any]
    links: dict[str, Any]
    phase4: dict[str, Any]
    warnings: list[Any]
    errors: list[dict[str, str]]

    @property
    def gates_passed(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class PublishArtifact:
    path: Path
    original: str
    candidate: str
    kind: str


@dataclass(frozen=True)
class LoadedPublishJournal:
    journal: dict[str, Any]
    raw: bytes
    phase: str
    path: Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SyncInputError(f"cannot inspect path identity: {path}: {exc}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    is_junction = getattr(path, "is_junction", None)
    try:
        junction = bool(callable(is_junction) and is_junction())
    except OSError as exc:
        raise SyncInputError(f"cannot inspect junction state: {path}: {exc}") from exc
    return path.is_symlink() or junction or bool(attributes & reparse_flag)


def _stable_identity(info: os.stat_result, label: str) -> tuple[int, int]:
    identity = (int(getattr(info, "st_dev", 0)), int(getattr(info, "st_ino", 0)))
    if identity == (0, 0):
        raise SyncInputError(f"{label} does not expose a stable filesystem identity")
    return identity


def _require_regular_file(path: Path, label: str) -> None:
    if _is_reparse(path):
        raise SyncInputError(
            f"{label} must not be a symlink, junction, or reparse point: {path}"
        )
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncInputError(f"{label} cannot be resolved: {path}: {exc}") from exc
    if not resolved.is_file():
        raise SyncInputError(f"{label} is not a regular file: {path}")


def _project_paths(root_value: str | os.PathLike[str]) -> ProjectPaths:
    supplied = Path(root_value)
    lexical_root = Path(os.path.abspath(supplied))
    try:
        root_state = lexical_root.lstat()
        root = lexical_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncInputError(
            f"project root cannot be resolved: {supplied}: {exc}"
        ) from exc
    if (
        _is_reparse(lexical_root)
        or not stat.S_ISDIR(root_state.st_mode)
        or os.path.normcase(os.path.normpath(str(lexical_root)))
        != os.path.normcase(os.path.normpath(str(root)))
    ):
        raise SyncInputError(
            f"project root must be a direct real directory: {supplied}"
        )
    root_identity = _stable_identity(root_state, "project root")

    lexical_docs = root / "project_docs"
    try:
        docs_state = lexical_docs.lstat()
        docs_root = lexical_docs.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncInputError(
            f"project_docs cannot be resolved: {lexical_docs}: {exc}"
        ) from exc
    if (
        _is_reparse(lexical_docs)
        or not stat.S_ISDIR(docs_state.st_mode)
        or docs_root.parent != root
        or os.path.normcase(os.path.normpath(str(lexical_docs)))
        != os.path.normcase(os.path.normpath(str(docs_root)))
    ):
        raise SyncInputError(
            "project_docs must be a direct real directory under project root"
        )
    docs_root_identity = _stable_identity(docs_state, "project_docs")

    claude_path = root / "CLAUDE.md"
    _require_regular_file(claude_path, "root CLAUDE.md")
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    scan_state_path = docs_root / SCAN_STATE_FILENAME
    for optional_path, label in (
        (manifest_path, "manifest"),
        (scan_state_path, "scan state"),
    ):
        if os.path.lexists(optional_path) and _is_reparse(optional_path):
            raise SyncInputError(
                f"{label} must not be a link or reparse point: {optional_path}"
            )
    return ProjectPaths(
        root,
        docs_root,
        claude_path,
        manifest_path,
        scan_state_path,
        root_identity,
        docs_root_identity,
    )


def _optional_bytes_with_identity(
    path: Path,
    *,
    label: str = "optional file",
    require_single_link: bool = False,
) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    """Read one optional no-follow file and return its stable identity."""

    try:
        before = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise SyncInputError(f"cannot inspect {label} {path}: {exc}") from exc
    if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
        raise SyncInputError(f"{label} must be direct and regular: {path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise SyncInputError(f"opened {label} is not a regular file: {path}")
        before_identity = phase4_exit._identity_from_stat(before)
        opened_identity = phase4_exit._identity_from_stat(opened)
        if opened_identity != before_identity:
            raise SyncInputError(f"{label} changed before open: {path}")
        if require_single_link and (
            int(getattr(before, "st_nlink", 1)) != 1
            or int(getattr(opened, "st_nlink", 1)) != 1
        ):
            raise SyncInputError(f"{label} must have exactly one filesystem link: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final_opened = os.fstat(descriptor)
        after = path.lstat()
    except FileNotFoundError as exc:
        raise SyncInputError(f"{label} disappeared while being read: {path}") from exc
    except OSError as exc:
        raise SyncInputError(f"cannot safely read {label} {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    final_identity = phase4_exit._identity_from_stat(final_opened)
    after_identity = phase4_exit._identity_from_stat(after)
    if (
        opened_identity != final_identity
        or opened_identity != after_identity
        or opened.st_size != final_opened.st_size
        or opened.st_size != after.st_size
        or getattr(opened, "st_mtime_ns", None)
        != getattr(final_opened, "st_mtime_ns", None)
        or getattr(opened, "st_mtime_ns", None) != getattr(after, "st_mtime_ns", None)
        or (
            require_single_link
            and (
                int(getattr(final_opened, "st_nlink", 1)) != 1
                or int(getattr(after, "st_nlink", 1)) != 1
            )
        )
    ):
        raise SyncInputError(f"{label} changed while being read: {path}")
    raw = b"".join(chunks)
    if len(raw) != opened.st_size:
        raise SyncInputError(f"{label} size changed while being read: {path}")
    return raw, opened_identity


def _optional_bytes(path: Path) -> bytes | None:
    """Read an optional regular file without accepting path replacement."""

    raw, _ = _optional_bytes_with_identity(path)
    return raw


def _normalized_marker_files(
    docs_root: Path,
) -> tuple[dict[str, str], dict[str, str], list[dict[str, Any]]]:
    changes = normalize_markers.collect_marker_changes(docs_root)
    contents: dict[str, str] = {}
    originals: dict[str, str] = {}
    details: list[dict[str, Any]] = []
    for change in changes:
        prior = contents.get(change.path)
        if prior is not None and prior != change.normalized_content:
            raise SyncInputError(
                f"inconsistent marker normalization candidates: {change.path}"
            )
        prior_original = originals.get(change.path)
        if prior_original is not None and prior_original != change.original_content:
            raise SyncInputError(f"inconsistent marker source snapshots: {change.path}")
        contents[change.path] = change.normalized_content
        originals[change.path] = change.original_content
        details.append(
            {
                "path": change.path,
                "line": change.line,
                "before": change.before,
                "after": change.after,
            }
        )
    details.sort(key=lambda item: (item["path"], item["line"]))
    return contents, originals, details


def _stage_documents(
    docs_root: Path,
    stage_docs: Path,
    captured_docs: Path,
    marker_contents: Mapping[str, str],
    marker_originals: Mapping[str, str],
) -> dict[str, str]:
    if set(marker_contents) != set(marker_originals):
        raise SyncInputError("marker candidate/source path sets are inconsistent")

    original_documents: dict[str, str] = {}
    discovered = link_check.discover_documents(docs_root)
    for relative, _ in discovered:
        content = contract.read_project_document(docs_root, relative)
        marker_original = marker_originals.get(relative)
        if marker_original is not None and content != marker_original:
            raise SyncInputError(
                f"marker source changed between collection and staging: {relative}"
            )
        original_documents[relative] = content
        parts = PurePosixPath(relative).parts
        captured_destination = captured_docs.joinpath(*parts)
        captured_destination.parent.mkdir(parents=True, exist_ok=True)
        captured_destination.write_bytes(content.encode("utf-8"))
        staged = marker_contents.get(relative, content)
        destination = stage_docs.joinpath(*parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(staged.encode("utf-8"))

    missing_candidates = set(marker_contents) - set(original_documents)
    if missing_candidates:
        raise SyncInputError(
            f"marker candidate documents disappeared during staging: {sorted(missing_candidates)}"
        )
    return original_documents


def _load_current_manifest(
    paths: ProjectPaths,
    captured_bytes: bytes | None,
    captured_docs: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    if captured_bytes is None:
        return None, ["existing manifest is missing; apply will create schema v2"]
    captured: list[str] = []
    try:
        try:
            text = captured_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise contract.StrictUtf8Error(
                f"existing manifest is not strict UTF-8: {exc}"
            ) from exc
        value = contract.parse_json_strict(
            text,
            source=str(paths.manifest_path),
        )
        if not isinstance(value, Mapping):
            raise contract.ManifestSchemaError("manifest JSON root must be an object")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            manifest = contract.validate_manifest(
                value,
                captured_docs,
                verify_files=True,
                verify_complete_set=True,
            )
        captured.extend(str(item.message) for item in caught)
        return manifest, captured
    except contract.ManifestSchemaError as exc:
        if "unsupported manifest schema_version" in str(exc):
            raise SyncInputError(
                f"existing manifest uses an unknown schema: {exc}"
            ) from exc
        captured.append(
            f"existing manifest is invalid and will be rebuilt on apply: {exc}"
        )
        return None, captured
    except contract.ProjectDocsContractError as exc:
        captured.append(
            f"existing manifest is invalid and will be rebuilt on apply: {exc}"
        )
        return None, captured


def _select_manifest(
    paths: ProjectPaths,
    stage_docs: Path,
    run_timestamp: str,
    current_manifest: Mapping[str, Any] | None,
    current_manifest_bytes: bytes | None,
) -> tuple[dict[str, Any], bool, str | None, int | None]:
    candidate = contract.build_manifest(
        stage_docs,
        last_synced=run_timestamp,
        docs_root_label=paths.docs_root.name,
    )
    current_generation: str | None = None
    current_schema: int | None = None
    if current_manifest is not None:
        schema_value = current_manifest.get("schema_version")
        current_schema = schema_value if type(schema_value) is int else None
        if current_schema == contract.MANIFEST_SCHEMA_VERSION:
            generation_value = current_manifest.get("generation_id")
            current_generation = (
                generation_value if isinstance(generation_value, str) else None
            )
            if current_generation == candidate["generation_id"]:
                # Keep the prior non-volatile snapshot byte-for-byte.  In particular,
                # do not refresh last_synced on a zero-change run.
                candidate = copy.deepcopy(dict(current_manifest))

    candidate_bytes = contract.stable_json_dumps(candidate).encode("utf-8")
    manifest_changed = current_manifest_bytes != candidate_bytes
    contract.atomic_write_json(stage_docs / contract.MANIFEST_FILENAME, candidate)
    contract.validate_manifest_v2(candidate, stage_docs, verify_files=True)
    return candidate, manifest_changed, current_generation, current_schema


def _scan_candidate(
    paths: ProjectPaths,
    captured_scan_state_bytes: bytes | None,
    *,
    force_full: bool,
    manifest_generation_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
    try:
        previous = scan_dirs.parse_previous_state_bytes(
            captured_scan_state_bytes,
            source=str(paths.scan_state_path),
        )
        baseline_precondition = scan_dirs.baseline_precondition_from_bytes(
            captured_scan_state_bytes,
            source=str(paths.scan_state_path),
        )
    except (OSError, ValueError, contract.ProjectDocsContractError) as exc:
        raise SyncInputError(f"committed scan state is invalid: {exc}") from exc
    candidate = scan_dirs.build_candidate_state(
        paths.root,
        previous,
        incremental=not force_full,
        force_full=force_full,
        baseline_precondition=baseline_precondition,
    )
    counts = candidate["change_counts"]
    changed = (
        previous is None
        or previous.get("schema_version") != scan_dirs.SCAN_STATE_SCHEMA_VERSION
    )
    changed = changed or any(
        counts[name] for name in ("new", "modified", "deleted", "unknown")
    )
    changed = changed or (
        previous is None
        or previous.get("manifest_generation_id") != manifest_generation_id
    )
    return previous, candidate, bool(changed)


def _strict_document_paths(scan_candidate: Mapping[str, Any]) -> set[str]:
    baseline = scan_candidate.get("baseline", {})
    if not isinstance(baseline, Mapping) or baseline.get("schema_version") != 2:
        return set()
    changes = scan_candidate.get("changes", {})
    paths: set[str] = set()
    if isinstance(changes, Mapping):
        for name in ("new", "modified"):
            values = changes.get(name, [])
            if isinstance(values, list):
                paths.update(
                    (f"{value}.md" if value else "index.md").casefold()
                    for value in values
                    if isinstance(value, str)
                )
    return paths


def _snapshot_directory_records(
    scan_candidate: Mapping[str, Any],
) -> list[dict[str, Any]]:
    snapshot = scan_candidate.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise SyncInputError("scan candidate snapshot is missing")
    records = snapshot.get("directories")
    if not isinstance(records, list) or any(
        not isinstance(item, Mapping) for item in records
    ):
        raise SyncInputError("scan candidate directory snapshot is malformed")
    return [dict(item) for item in records]


def _stage_source_snapshot(
    stage_root: Path,
    scan_candidate: Mapping[str, Any],
) -> set[str]:
    """Materialize only names/inventories captured by the stable source scan."""

    known_paths: set[str] = set()
    for record in _snapshot_directory_records(scan_candidate):
        relative = str(record["path"])
        source_dir = stage_root.joinpath(*PurePosixPath(relative).parts)
        source_dir.mkdir(parents=True, exist_ok=True)
        known_paths.add(relative.casefold())
        snapshot = record.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise SyncInputError(f"source snapshot is malformed: {relative}")
        children = snapshot.get("child_directories", [])
        files = snapshot.get("files", [])
        if not isinstance(children, list) or not isinstance(files, list):
            raise SyncInputError(f"source inventory is malformed: {relative}")
        for child in children:
            if not isinstance(child, str):
                raise SyncInputError(f"source child name is malformed: {relative}")
            (source_dir / child).mkdir(exist_ok=True)
        for file_info in files:
            if not isinstance(file_info, Mapping) or not isinstance(
                file_info.get("name"), str
            ):
                raise SyncInputError(f"source file snapshot is malformed: {relative}")
            source_file = source_dir / str(file_info["name"])
            if source_file.exists() and source_file.is_dir():
                raise SyncInputError(
                    f"source snapshot has a file/directory collision: {relative}"
                )
            source_file.write_bytes(b"")
    return known_paths


def _phase4_source_inventory(
    scan_candidate: Mapping[str, Any],
) -> phase4_exit.SourceInventory:
    records = _snapshot_directory_records(scan_candidate)
    directories = {str(record["path"]) for record in records}
    boundaries = {
        str(record["path"]) for record in records if record.get("is_boundary") is True
    }
    snapshot = scan_candidate.get("snapshot")
    unknown_value = (
        snapshot.get("unknown_scopes", []) if isinstance(snapshot, Mapping) else []
    )
    if not isinstance(unknown_value, list) or any(
        not isinstance(item, Mapping) for item in unknown_value
    ):
        raise SyncInputError("scan candidate unknown scopes are malformed")
    unknown_scopes = tuple(dict(item) for item in unknown_value)
    return phase4_exit.SourceInventory(
        directories=directories,
        boundaries=boundaries,
        unknown_scopes=unknown_scopes,
        frozen=True,
    )


def _compact_failed_checks(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = result.get("checks", [])
    if not isinstance(checks, list):
        return []
    return [
        {
            "id": check.get("id"),
            "name": check.get("name"),
            "severity": check.get("severity"),
            "detail": check.get("detail"),
        }
        for check in checks
        if isinstance(check, Mapping) and check.get("result") == "FAIL"
    ]


def _validate_documents(
    source_snapshot_root: Path,
    known_source_paths: set[str],
    stage_docs: Path,
    manifest: Mapping[str, Any],
    scan_candidate: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    entries = [dict(entry) for entry in manifest["files"]]
    by_folded_path = {entry["path"].casefold(): entry for entry in entries}
    strict_paths = _strict_document_paths(scan_candidate)
    blocking: list[dict[str, Any]] = []
    audit_failures: list[dict[str, Any]] = []
    skipped_missing_source: list[str] = []

    for strict_path in sorted(strict_paths):
        if strict_path not in by_folded_path:
            blocking.append(
                {
                    "path": strict_path,
                    "error_code": "changed-directory-document-missing",
                    "failed_checks": [],
                }
            )

    def _validate_one(
        entry: dict[str, Any],
    ) -> tuple[str, bool, dict[str, Any] | None]:
        relative = entry["path"]
        source_relative = "" if relative.casefold() == "index.md" else relative[:-3]
        if source_relative.casefold() not in known_source_paths:
            return relative, True, None
        source_dir = source_snapshot_root.joinpath(
            *PurePosixPath(source_relative).parts
        )
        if not source_dir.is_dir():
            raise SyncInputError(
                f"frozen source snapshot is missing a known directory: {source_relative}"
            )
        staged_doc = stage_docs.joinpath(*PurePosixPath(relative).parts)
        result = validate_doc.validate_doc(
            staged_doc,
            source_dir,
            project_docs_dir=stage_docs,
            project_root=source_snapshot_root,
        )
        summary = None
        if result["failed"] or result["error_code"] is not None:
            summary = {
                "path": relative,
                "profile": result["profile"],
                "failed": result["failed"],
                "critical_failed": result["critical_failed"],
                "error_code": result["error_code"],
                "error": result["error"],
                "failed_checks": _compact_failed_checks(result),
            }
        return relative, False, summary

    if len(entries) > 1:
        max_workers = min(32, (os.cpu_count() or 4) * 4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            validation_results = list(executor.map(_validate_one, entries))
    else:
        validation_results = [_validate_one(entry) for entry in entries]

    for relative, is_skipped, summary in validation_results:
        if is_skipped:
            skipped_missing_source.append(relative)
            continue
        if summary is not None:
            audit_failures.append(summary)
            if relative.casefold() in strict_paths:
                blocking.append(summary)

    audit_failures.sort(key=lambda item: item["path"])
    blocking.sort(key=lambda item: item["path"])
    skipped_missing_source.sort()
    report = {
        "strict_mode_enabled": bool(strict_paths),
        "strict_paths": sorted(strict_paths),
        "checked": len(entries) - len(skipped_missing_source),
        "audit_failure_count": len(audit_failures),
        "audit_failures": audit_failures,
        "blocking_failure_count": len(blocking),
        "blocking_failures": blocking,
        "skipped_missing_source": skipped_missing_source,
    }
    errors = [
        {
            "code": "changed-document-validation-failed",
            "message": f"{item['path']} failed strict changed-document validation",
        }
        for item in blocking
    ]
    return report, errors


def _phase4_report(
    source_snapshot_root: Path,
    stage_docs: Path,
    manifest: Mapping[str, Any],
    source_inventory: phase4_exit.SourceInventory,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    # Bind Phase 4 to the same stable scanner inventory used by changed-document
    # validation.  It must not independently observe a transient live source tree.
    analysis = phase4_exit.analyze_snapshot(
        source_snapshot_root,
        stage_docs,
        dict(manifest),
        source_inventory=source_inventory,
    )
    result = phase4_exit.analysis_result(analysis, action="report", mode="report-only")
    errors = [dict(item) for item in result.get("errors", [])]
    warnings: list[dict[str, str]] = []
    counts = result.get("counts", {})
    candidate_count = counts.get("candidates", 0) if isinstance(counts, Mapping) else 0
    if type(candidate_count) is int and candidate_count > 0:
        # Pending candidates must not deadlock the quarantine workflow: the
        # reference-clearing document edits need a published manifest before
        # ``phase4_exit --apply`` accepts the transaction.  Documents, the
        # CLAUDE index, and the manifest may still publish; only the scan
        # baseline stays frozen until quarantine completes.
        warnings.append(
            {
                "code": "phase4-candidates-pending",
                "severity": "warning",
                "message": (
                    f"{candidate_count} Phase 4 candidate(s) require a separate, "
                    "explicit quarantine decision before the scan baseline can advance"
                ),
            }
        )
    return result, errors, warnings


def _documents_unchanged(prepared: PreparedSync) -> None:
    discovered = link_check.discover_documents(prepared.paths.docs_root)
    current_paths = {relative for relative, _ in discovered}
    expected_paths = set(prepared.original_documents)
    if current_paths != expected_paths:
        raise SyncInputError(
            "document inventory changed during staging: "
            f"added={sorted(current_paths - expected_paths)}, "
            f"removed={sorted(expected_paths - current_paths)}"
        )
    for relative in sorted(expected_paths):
        current = contract.read_project_document(prepared.paths.docs_root, relative)
        if current != prepared.original_documents[relative]:
            raise SyncInputError(f"document changed during staging: {relative}")


def _documents_match_candidate(prepared: PreparedSync) -> None:
    discovered = link_check.discover_documents(prepared.paths.docs_root)
    current_paths = {relative for relative, _ in discovered}
    expected_paths = set(prepared.original_documents)
    if current_paths != expected_paths:
        raise SyncInputError(
            "document inventory changed before manifest commit: "
            f"added={sorted(current_paths - expected_paths)}, "
            f"removed={sorted(expected_paths - current_paths)}"
        )
    for relative in sorted(expected_paths):
        expected = prepared.marker_contents.get(
            relative,
            prepared.original_documents[relative],
        )
        current = contract.read_project_document(prepared.paths.docs_root, relative)
        if current != expected:
            raise SyncInputError(f"document changed before manifest commit: {relative}")


def _verify_committed_manifest(prepared: PreparedSync) -> None:
    expected = contract.stable_json_dumps(prepared.manifest).encode("utf-8")
    if _optional_bytes(prepared.paths.manifest_path) != expected:
        raise SyncInputError(
            "manifest commit point does not contain the prepared bytes"
        )
    loaded = contract.load_manifest(
        prepared.paths.manifest_path,
        docs_root=prepared.paths.docs_root,
        verify_files=True,
        verify_complete_set=True,
    )
    if loaded.get("generation_id") != prepared.manifest.get("generation_id"):
        raise SyncInputError("manifest generation changed during commit verification")


def _source_snapshot_unchanged(prepared: PreparedSync) -> None:
    fresh = scan_dirs.build_candidate_state(
        prepared.paths.root,
        prepared.previous_scan_state,
        incremental=not prepared.force_full,
        force_full=prepared.force_full,
    )
    if fresh["snapshot_fingerprint"] != prepared.scan_candidate["snapshot_fingerprint"]:
        raise SyncInputError("source directory snapshot changed during staging")


def _require_directory_identity(
    path: Path,
    expected: tuple[int, int],
    label: str,
) -> None:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncInputError(f"cannot revalidate {label}: {path}: {exc}") from exc
    if (
        _is_reparse(path)
        or not stat.S_ISDIR(info.st_mode)
        or os.path.normcase(os.path.normpath(str(path)))
        != os.path.normcase(os.path.normpath(str(resolved)))
        or _stable_identity(info, label) != expected
    ):
        raise SyncInputError(f"{label} changed identity or became indirect: {path}")


def _revalidate_project_path_identities(paths: ProjectPaths) -> None:
    _require_directory_identity(paths.root, paths.root_identity, "project root")
    _require_directory_identity(
        paths.docs_root,
        paths.docs_root_identity,
        "project_docs",
    )
    if paths.docs_root.parent != paths.root:
        raise SyncInputError("project_docs parent changed during synchronization")


def _revalidate_persistent_path_types(paths: ProjectPaths) -> None:
    _revalidate_project_path_identities(paths)
    _require_regular_file(paths.claude_path, "root CLAUDE.md")
    for optional_path, label in (
        (paths.manifest_path, "manifest"),
        (paths.scan_state_path, "scan state"),
    ):
        if not os.path.lexists(optional_path):
            continue
        if _is_reparse(optional_path):
            raise SyncInputError(
                f"{label} must not be a link or reparse point: {optional_path}"
            )
        try:
            info = optional_path.lstat()
        except OSError as exc:
            raise SyncInputError(
                f"cannot inspect {label}: {optional_path}: {exc}"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise SyncInputError(f"{label} is not a regular file: {optional_path}")


def _persistent_inputs_unchanged(prepared: PreparedSync) -> None:
    _revalidate_persistent_path_types(prepared.paths)
    if contract.read_utf8(prepared.paths.claude_path) != prepared.original_claude:
        raise SyncInputError("CLAUDE.md changed during staging")
    if (
        _optional_bytes(prepared.paths.manifest_path)
        != prepared.original_manifest_bytes
    ):
        raise SyncInputError("manifest changed during staging")
    if (
        _optional_bytes(prepared.paths.scan_state_path)
        != prepared.original_scan_state_bytes
    ):
        raise SyncInputError("scan state changed during staging")
    _source_snapshot_unchanged(prepared)


def _atomic_text(path: Path, content: str) -> None:
    contract.atomic_write_utf8(path, content)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    contract.atomic_write_json(path, dict(value))


def _publish_fault(point: str) -> None:
    """测试故障注入点；生产运行不执行任何操作。"""

    return None


@contextmanager
def _project_lock_preserving_primary(
    root: Path,
    *,
    timeout: float,
    secondary_errors: list[dict[str, str]] | None = None,
) -> Any:
    """Never let lock-release failure replace an in-flight publish failure."""

    manager = contract.project_sync_lock(root, timeout=timeout)
    manager.__enter__()
    try:
        yield
    except BaseException as primary:
        try:
            manager.__exit__(type(primary), primary, primary.__traceback__)
        except BaseException as release_exc:
            if secondary_errors is not None:
                secondary_errors.append(
                    {
                        "stage": "project-lock-release",
                        "error": str(release_exc),
                    }
                )
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                add_note(f"project lock release also failed: {release_exc}")
        raise
    else:
        manager.__exit__(None, None, None)


def _build_committed_scan_state(
    candidate: Mapping[str, Any],
    state_path: Path,
    manifest_generation_id: str,
) -> dict[str, Any]:
    committed = copy.deepcopy(dict(candidate))
    committed["state_role"] = "committed"
    committed["committed_at"] = _utc_now()
    committed["directories"] = copy.deepcopy(committed["snapshot"]["directories"])
    committed["total"] = committed["full_total"]
    committed.pop("candidate_path", None)
    committed["state_path"] = state_path.name
    committed["manifest_generation_id"] = manifest_generation_id
    return scan_dirs.validate_scan_state_v2(committed)


def _serialized_identity(
    identity: tuple[int, int, int, int],
) -> list[int]:
    return [int(component) for component in identity]


def _decode_serialized_identity(
    value: Any,
    label: str,
    *,
    expected_file_type: int,
) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(component) is not int for component in value)
    ):
        raise SyncInputError(f"{label} must be a four-integer filesystem identity")
    identity = tuple(int(component) for component in value)
    if identity[0] < 0 or identity[1] <= 0:
        raise SyncInputError(f"{label} has no stable device/inode identity")
    if identity[2] != expected_file_type:
        raise SyncInputError(f"{label} has an unexpected filesystem object type")
    return identity


def _bytes_snapshot(value: bytes | None) -> dict[str, Any]:
    return {
        "exists": value is not None,
        "sha256": contract.sha256_bytes(value) if value is not None else None,
        "base64": (
            base64.b64encode(value).decode("ascii") if value is not None else None
        ),
    }


def _decode_bytes_snapshot(value: Any, label: str) -> bytes | None:
    if not isinstance(value, Mapping) or set(value) != {"exists", "sha256", "base64"}:
        raise SyncInputError(f"{label} snapshot has an invalid schema")
    exists = value.get("exists")
    digest = value.get("sha256")
    encoded = value.get("base64")
    if type(exists) is not bool:
        raise SyncInputError(f"{label}.exists must be a boolean")
    if not exists:
        if digest is not None or encoded is not None:
            raise SyncInputError(f"absent {label} snapshot cannot carry bytes")
        return None
    if not isinstance(digest, str) or len(digest) != 64:
        raise SyncInputError(f"{label}.sha256 is invalid")
    if not isinstance(encoded, str):
        raise SyncInputError(f"{label}.base64 is invalid")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise SyncInputError(f"{label}.base64 is invalid") from exc
    if contract.sha256_bytes(raw) != digest:
        raise SyncInputError(f"{label} snapshot hash does not match its bytes")
    return raw


def _publish_journal_path(paths: ProjectPaths) -> Path:
    return contract.coordinator_publish_journal_paths(paths.root)["prepared"]


def _publish_artifact_root_name(journal_path: Path, transaction_id: str) -> str:
    return f"{journal_path.stem}{_PUBLISH_ARTIFACT_ROOT_MARKER}{transaction_id}"


def _publish_artifact_root(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> Path:
    transaction_id = journal.get("transaction_id")
    root_name = journal.get("artifact_root")
    if not isinstance(transaction_id, str) or not isinstance(root_name, str):
        raise SyncInputError("publish recovery journal has no artifact-root binding")
    expected = _publish_artifact_root_name(_publish_journal_path(paths), transaction_id)
    if root_name != expected or Path(root_name).name != root_name:
        raise SyncInputError("publish recovery journal artifact root is invalid")
    return _publish_journal_path(paths).parent / root_name


def _filesystem_anchor(path: Path) -> Path:
    anchor = Path(path.anchor)
    if not anchor.is_dir():
        raise SyncInputError(f"filesystem anchor is unavailable: {anchor}")
    return anchor


def _create_publish_artifact_root(
    paths: ProjectPaths,
    transaction_id: str,
) -> tuple[Path, tuple[int, int, int, int]]:
    """Create the CAS directory before publishing its immutable identity binding."""

    journal_path = _publish_journal_path(paths)
    artifact_root = journal_path.parent / _publish_artifact_root_name(
        journal_path,
        transaction_id,
    )
    trusted_root = _filesystem_anchor(paths.root)
    try:
        parent_state = artifact_root.parent.lstat()
        root_state = paths.root.lstat()
        phase4_exit._validate_existing_components(
            trusted_root,
            artifact_root.parent,
            "coordinator journal directory",
        )
        if int(parent_state.st_dev) != int(root_state.st_dev):
            raise SyncInputError(
                "coordinator journal storage and project root must share a filesystem "
                "for identity-preserving publication"
            )
        artifact_root.mkdir(mode=0o700)
        phase4_exit._validate_existing_components(
            trusted_root,
            artifact_root,
            "coordinator artifact root",
        )
        info = artifact_root.lstat()
    except FileExistsError as exc:
        raise SyncInputError(
            f"coordinator artifact root already exists: {artifact_root}"
        ) from exc
    except (OSError, phase4_exit.Phase4Refusal) as exc:
        detail = exc.message if isinstance(exc, phase4_exit.Phase4Refusal) else str(exc)
        raise SyncInputError(
            f"cannot create coordinator artifact root: {detail}"
        ) from exc
    if _is_reparse(artifact_root) or not stat.S_ISDIR(info.st_mode):
        raise SyncInputError(
            f"coordinator artifact root is not a direct directory: {artifact_root}"
        )
    identity = phase4_exit._identity_from_stat(info)
    if identity[0] != int(root_state.st_dev) or identity[1] <= 0:
        raise SyncInputError(
            "coordinator artifact root has no stable same-filesystem identity"
        )
    _fsync_parent_directory(artifact_root.parent)
    return artifact_root, identity


def _publish_artifact_root_expected_identity(
    journal: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    return _decode_serialized_identity(
        journal.get("artifact_root_identity"),
        "publish recovery journal artifact_root_identity",
        expected_file_type=stat.S_IFDIR,
    )


def _ensure_publish_artifact_root(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> Path:
    """Revalidate the exact transaction-owned CAS artifact directory."""

    artifact_root = _publish_artifact_root(paths, journal)
    expected_identity = _publish_artifact_root_expected_identity(journal)
    trusted_root = _filesystem_anchor(paths.root)
    try:
        phase4_exit._validate_existing_components(
            trusted_root,
            artifact_root,
            "coordinator artifact root",
        )
        info = artifact_root.lstat()
    except (OSError, phase4_exit.Phase4Refusal) as exc:
        detail = exc.message if isinstance(exc, phase4_exit.Phase4Refusal) else str(exc)
        raise SyncInputError(
            f"cannot validate coordinator artifact root: {detail}"
        ) from exc
    if (
        _is_reparse(artifact_root)
        or not stat.S_ISDIR(info.st_mode)
        or phase4_exit._identity_from_stat(info) != expected_identity
    ):
        raise SyncInputError(
            f"coordinator artifact root identity changed: {artifact_root}"
        )
    return artifact_root


def _phase4_text_snapshot(raw: bytes | None, label: str) -> dict[str, Any]:
    if raw is None:
        return phase4_exit._absent_snapshot()
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SyncInputError(f"{label} is not strict UTF-8") from exc
    return phase4_exit._text_snapshot(text)


def _set_publish_path_state(
    path: Path,
    before: bytes | None,
    after: bytes | None,
    *,
    desired_after: bool,
    allow_both: bool,
    label: str,
    paths: ProjectPaths,
    artifact_root: Path,
    artifact_root_identity: tuple[int, int, int, int],
) -> None:
    """Publish or recover one exact snapshot through detach/no-replace CAS."""

    before_snapshot = _phase4_text_snapshot(before, f"{label} preimage")
    after_snapshot = _phase4_text_snapshot(after, f"{label} postimage")
    desired = after_snapshot if desired_after else before_snapshot
    allowed = [before_snapshot, after_snapshot] if allow_both else [before_snapshot]
    try:
        phase4_exit._set_snapshot_state(
            path,
            paths.root,
            label,
            desired,
            allowed,
            archive_root=artifact_root,
            archive_trusted_root=_filesystem_anchor(paths.root),
            archive_root_expected_identity=artifact_root_identity,
        )
    except (phase4_exit.Phase4Refusal, phase4_exit.Phase4OperationalError) as exc:
        raise SyncInputError(f"{label} CAS refused [{exc.code}]: {exc.message}") from exc
    current = _optional_bytes(path)
    expected = after if desired_after else before
    if current != expected:
        raise SyncInputError(f"{label} CAS failed exact readback")


def _read_publish_journal_bytes(path: Path) -> bytes | None:
    try:
        raw, _ = _optional_bytes_with_identity(
            path,
            label="publish journal",
            require_single_link=True,
        )
        return raw
    except SyncInputError as exc:
        raise SyncInputError(
            f"cannot safely read publish journal {path}: {exc}"
        ) from exc


def _fsync_parent_directory(path: Path) -> None:
    """Persist directory namespace after mutation, identity-bound on all platforms."""

    phase4_exit._fsync_directory(path)


def _cleanup_publish_journal_temps(journal_path: Path) -> None:
    prefix = f"{journal_path.name}{_PUBLISH_JOURNAL_TEMP_MARKER}"
    try:
        entries = list(journal_path.parent.iterdir())
    except OSError as exc:
        raise SyncInputError(
            f"cannot inspect publish journal directory: {exc}"
        ) from exc
    residues: list[str] = []
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.name.endswith(".tmp"):
            continue
        if _is_reparse(entry):
            raise SyncInputError(f"publish journal temporary path is indirect: {entry}")
        try:
            info = entry.lstat()
        except OSError as exc:
            raise SyncInputError(
                f"cannot inspect publish journal temporary file: {entry}"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise SyncInputError(
                f"publish journal temporary path is not a file: {entry}"
            )
        residues.append(entry.name)
    if residues:
        raise SyncInputError(
            "unowned publish journal temporary residue requires audited recovery: "
            f"{sorted(residues)}"
        )


def _publish_journal_exclusive(
    path: Path,
    value: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    if _read_publish_journal_bytes(path) is not None:
        raise SyncInputError(f"a publish recovery journal already exists: {path}")
    _cleanup_publish_journal_temps(path)
    if "journal_identity" in value:
        raise SyncInputError("publish journal draft already contains journal_identity")
    temporary = path.with_name(
        f"{path.name}{_PUBLISH_JOURNAL_TEMP_MARKER}{uuid.uuid4().hex}.tmp"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        opened = os.fstat(descriptor)
        identity = phase4_exit._identity_from_stat(opened)
        if (
            identity[1] <= 0
            or identity[2] != stat.S_IFREG
            or int(getattr(opened, "st_nlink", 1)) != 1
        ):
            raise SyncInputError(
                "publish journal temporary file has no exclusive stable identity"
            )
        journal = dict(value)
        journal["journal_identity"] = _serialized_identity(identity)
        expected = contract.stable_json_dumps(journal).encode("utf-8")
        offset = 0
        while offset < len(expected):
            written = os.write(descriptor, expected[offset:])
            if written <= 0:
                raise OSError("short publish journal write")
            offset += written
        os.fsync(descriptor)
        final_opened = os.fstat(descriptor)
        if (
            phase4_exit._identity_from_stat(final_opened) != identity
            or int(getattr(final_opened, "st_nlink", 1)) != 1
        ):
            raise SyncInputError("publish journal temporary identity changed during write")
        os.close(descriptor)
        descriptor = None
        temporary_raw, temporary_identity = _optional_bytes_with_identity(
            temporary,
            label="publish journal temporary file",
            require_single_link=True,
        )
        if temporary_raw != expected or temporary_identity != identity:
            raise SyncInputError("publish journal temporary snapshot failed readback")
        try:
            phase4_exit._rename_no_replace(temporary, path)
        except FileExistsError as exc:
            raise SyncInputError(
                f"a concurrent publish recovery journal already exists: {path}"
            ) from exc
        except OSError as exc:
            raise SyncInputError(
                f"cannot publish coordinator journal without replacement: {exc}"
            ) from exc
        canonical_raw, canonical_identity = _optional_bytes_with_identity(
            path,
            label="canonical publish journal",
            require_single_link=True,
        )
        if canonical_raw != expected or canonical_identity != identity:
            raise SyncInputError("canonical publish journal failed exact readback")
        _fsync_parent_directory(path.parent)
        return journal, expected
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            # Unknown/replaced residues are deliberately retained for audited
            # recovery.  Remove only the exact inode this invocation created.
            try:
                current_raw, current_identity = _optional_bytes_with_identity(
                    temporary,
                    label="publish journal temporary cleanup",
                    require_single_link=True,
                )
                if current_identity == locals().get("identity"):
                    expected_snapshot = _phase4_text_snapshot(
                        current_raw,
                        "publish journal temporary cleanup",
                    )
                    phase4_exit._remove_snapshot_file(
                        temporary,
                        _filesystem_anchor(path),
                        "publish journal temporary cleanup",
                        expected_snapshot,
                        expected_identity=current_identity,
                        archive_root=temporary.parent,
                        archive_trusted_root=_filesystem_anchor(path),
                    )
            except BaseException:
                pass


def _retained_publish_artifact_root(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> Path:
    transaction_id = str(journal["transaction_id"])
    prepared = contract.coordinator_publish_journal_paths(paths.root)["prepared"]
    return prepared.parent / (
        f"{prepared.stem}{_PUBLISH_RETAINED_ARTIFACT_MARKER}{transaction_id}"
    )


def _publish_history_path(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
    phase: str,
) -> Path:
    if phase not in {"committed", "rolled_back"}:
        raise SyncInputError(f"unsupported publish finalization phase: {phase}")
    transaction_id = str(journal["transaction_id"])
    prepared = contract.coordinator_publish_journal_paths(paths.root)["prepared"]
    return prepared.parent / (
        f"{prepared.stem}{_PUBLISH_HISTORY_MARKER}{transaction_id}-{phase}.json"
    )


def _journal_expected_identity(
    journal: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    return _decode_serialized_identity(
        journal.get("journal_identity"),
        "publish recovery journal journal_identity",
        expected_file_type=stat.S_IFREG,
    )


def _capture_exact_publish_journal(
    path: Path,
    journal: Mapping[str, Any],
    expected: bytes,
) -> tuple[int, int, int, int]:
    raw, identity = _optional_bytes_with_identity(
        path,
        label="publish journal",
        require_single_link=True,
    )
    if raw != expected or identity is None:
        raise SyncInputError(f"publish journal changed before finalization: {path}")
    expected_identity = _journal_expected_identity(journal)
    if identity != expected_identity:
        raise SyncInputError(
            f"publish journal identity changed before finalization: {path}"
        )
    return identity


def _transition_publish_journal(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
    expected: bytes,
    phase: str,
) -> LoadedPublishJournal:
    """Atomically move prepared intent into an immutable outcome slot."""

    slots = contract.coordinator_publish_journal_paths(paths.root)
    if phase not in {"committed", "rolled_back"}:
        raise SyncInputError(f"unsupported publish finalization phase: {phase}")
    source = slots["prepared"]
    destination = slots[phase]
    for other_phase, other_path in slots.items():
        if other_phase != "prepared" and os.path.lexists(other_path):
            raise SyncInputError(
                f"publish finalization slot is already occupied: {other_path}"
            )
    expected_identity = _capture_exact_publish_journal(source, journal, expected)
    _publish_fault(f"journal-finalize-{phase}-before-rename")
    try:
        phase4_exit._rename_no_replace(source, destination)
    except FileExistsError as exc:
        raise SyncInputError(
            f"publish finalization destination appeared concurrently: {destination}"
        ) from exc
    except OSError as exc:
        raise SyncInputError(f"cannot publish {phase} journal outcome: {exc}") from exc
    _publish_fault(f"journal-finalize-{phase}-after-rename")
    moved_identity = _capture_exact_publish_journal(destination, journal, expected)
    if moved_identity != expected_identity or os.path.lexists(source):
        raise SyncInputError(
            f"publish {phase} journal identity changed during transition"
        )
    _fsync_parent_directory(destination.parent)
    _publish_fault(f"journal-finalize-{phase}-durable")
    return LoadedPublishJournal(dict(journal), expected, phase, destination)


def _retire_publish_artifact_root(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> dict[str, str] | None:
    """Delete an empty Windows CAS root or retain the exact directory elsewhere."""

    active = _publish_artifact_root(paths, journal)
    retained = _retained_publish_artifact_root(paths, journal)
    expected_identity = _decode_serialized_identity(
        journal.get("artifact_root_identity"),
        "publish recovery journal artifact_root_identity",
        expected_file_type=stat.S_IFDIR,
    )
    trusted_root = _filesystem_anchor(paths.root)

    if os.name == "nt":
        if os.path.lexists(retained):
            raise SyncInputError(
                f"unexpected retained Windows coordinator artifact root: {retained}"
            )
        if not os.path.lexists(active):
            return None
        artifact_root = _ensure_publish_artifact_root(paths, journal)
        try:
            members = list(artifact_root.iterdir())
        except OSError as exc:
            raise SyncInputError(
                f"cannot inspect coordinator artifact root before cleanup: {exc}"
            ) from exc
        if members:
            raise SyncInputError(
                "coordinator artifact root is not empty after transaction completion: "
                f"{sorted(member.name for member in members)}"
            )
        try:
            phase4_exit._delete_empty_directory_by_handle_windows(
                artifact_root,
                expected_identity,
            )
        except OSError as exc:
            raise SyncInputError(
                f"cannot delete exact coordinator artifact root: {exc}"
            ) from exc
        if os.path.lexists(artifact_root):
            raise SyncInputError(
                f"coordinator artifact root remained after cleanup: {artifact_root}"
            )
        _fsync_parent_directory(artifact_root.parent)
        return None

    active_exists = os.path.lexists(active)
    retained_exists = os.path.lexists(retained)
    if active_exists and retained_exists:
        raise SyncInputError(
            "both active and retained coordinator artifact roots exist"
        )
    if retained_exists:
        try:
            phase4_exit._validate_existing_components(
                trusted_root,
                retained,
                "retained coordinator artifact root",
            )
            retained_identity = phase4_exit._identity_from_stat(retained.lstat())
        except (OSError, phase4_exit.Phase4Refusal) as exc:
            detail = exc.message if isinstance(exc, phase4_exit.Phase4Refusal) else str(exc)
            raise SyncInputError(
                f"cannot validate retained coordinator artifact root: {detail}"
            ) from exc
        if retained_identity != expected_identity:
            raise SyncInputError(
                "retained coordinator artifact root identity changed"
            )
        return {
            "kind": "coordinator-cas-artifacts",
            "path": retained.as_posix(),
            "reason": "identity-bound unlink is not portable on this platform",
        }
    if not active_exists:
        raise SyncInputError("coordinator artifact root disappeared before retention")
    artifact_root = _ensure_publish_artifact_root(paths, journal)
    try:
        phase4_exit._rename_no_replace(artifact_root, retained)
    except FileExistsError as exc:
        raise SyncInputError(
            f"retained coordinator artifact root appeared concurrently: {retained}"
        ) from exc
    except OSError as exc:
        raise SyncInputError(
            f"cannot retain coordinator artifact root safely: {exc}"
        ) from exc
    try:
        retained_identity = phase4_exit._identity_from_stat(retained.lstat())
    except OSError as exc:
        raise SyncInputError(
            f"cannot inspect retained coordinator artifact root: {exc}"
        ) from exc
    if retained_identity != expected_identity or os.path.lexists(artifact_root):
        raise SyncInputError(
            "coordinator artifact root identity changed during retention"
        )
    _fsync_parent_directory(retained.parent)
    return {
        "kind": "coordinator-cas-artifacts",
        "path": retained.as_posix(),
        "reason": "identity-bound unlink is not portable on this platform",
    }


def _retire_publish_finalization_journal(
    paths: ProjectPaths,
    loaded: LoadedPublishJournal,
) -> Path:
    """Move the immutable outcome out of its fixed recovery slot, never unlink it."""

    history = _publish_history_path(paths, loaded.journal, loaded.phase)
    expected_identity = _capture_exact_publish_journal(
        loaded.path,
        loaded.journal,
        loaded.raw,
    )
    if os.path.lexists(history):
        raise SyncInputError(f"publish history path already exists: {history}")
    _publish_fault("journal-finalize-before-history")
    try:
        phase4_exit._rename_no_replace(loaded.path, history)
    except FileExistsError as exc:
        raise SyncInputError(
            f"publish history appeared concurrently: {history}"
        ) from exc
    except OSError as exc:
        raise SyncInputError(f"cannot retain immutable publish history: {exc}") from exc
    _publish_fault("journal-finalize-after-history")
    history_identity = _capture_exact_publish_journal(
        history,
        loaded.journal,
        loaded.raw,
    )
    if history_identity != expected_identity or os.path.lexists(loaded.path):
        raise SyncInputError("publish journal identity changed during history retention")
    _fsync_parent_directory(history.parent)
    return history


def _require_terminal_publish_state(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
    phase: str,
) -> None:
    """Bind a terminal journal slot to the exact live project generation."""

    if phase not in {"committed", "rolled_back"}:
        raise SyncInputError(f"unsupported terminal publish phase: {phase}")
    use_after = phase == "committed"
    _revalidate_project_path_identities(paths)

    for artifact in _journal_artifacts(paths, journal):
        expected = (
            artifact.candidate.encode("utf-8")
            if use_after
            else artifact.original.encode("utf-8")
        )
        if _optional_bytes(artifact.path) != expected:
            raise SyncInputError(
                f"{phase} publish outcome does not match {artifact.kind}: "
                f"{artifact.path}"
            )

    manifest = journal["manifest"]
    manifest_before = _decode_bytes_snapshot(manifest["before"], "manifest.before")
    manifest_after = _decode_bytes_snapshot(manifest["after"], "manifest.after")
    expected_manifest = manifest_after if use_after else manifest_before
    if _optional_bytes(paths.manifest_path) != expected_manifest:
        raise SyncInputError(
            f"{phase} publish outcome does not match the recorded manifest"
        )

    scan = journal["scan_state"]
    scan_before = _decode_bytes_snapshot(scan["before"], "scan_state.before")
    scan_after = _decode_bytes_snapshot(scan["after"], "scan_state.after")
    expected_scan = scan_after if use_after and bool(scan["needed"]) else scan_before
    if _optional_bytes(paths.scan_state_path) != expected_scan:
        raise SyncInputError(
            f"{phase} publish outcome does not match the recorded scan state"
        )

    if expected_manifest is not None:
        loaded_manifest = contract.load_manifest(
            paths.manifest_path,
            docs_root=paths.docs_root,
            verify_files=True,
            verify_complete_set=True,
        )
        if use_after and loaded_manifest.get("generation_id") != journal.get(
            "manifest_generation_id"
        ):
            raise SyncInputError(
                "committed publish outcome has an unexpected manifest generation"
            )

    if expected_scan is not None:
        try:
            parsed_scan = scan_dirs.parse_previous_state_bytes(
                expected_scan,
                source=f"{phase} publish outcome scan state",
            )
        except (ValueError, contract.ProjectDocsContractError) as exc:
            raise SyncInputError(
                f"{phase} publish outcome scan state is invalid: {exc}"
            ) from exc
        if parsed_scan is None:
            raise SyncInputError(f"{phase} publish outcome scan state is absent")
        if use_after and bool(scan["needed"]):
            bindings = {
                "candidate_generation_id": journal["candidate_generation_id"],
                "snapshot_fingerprint": journal["source_snapshot_fingerprint"],
                "manifest_generation_id": journal["manifest_generation_id"],
            }
            for field, expected in bindings.items():
                if parsed_scan.get(field) != expected:
                    raise SyncInputError(
                        f"committed publish outcome scan-state {field} is inconsistent"
                    )

    if use_after:
        fresh_source = scan_dirs.build_candidate_state(paths.root)
        if (
            fresh_source.get("snapshot_fingerprint")
            != journal["source_snapshot_fingerprint"]
        ):
            raise SyncInputError(
                "source snapshot changed before committed journal finalization"
            )
    _revalidate_project_path_identities(paths)



def _finalize_publish_journal(
    paths: ProjectPaths,
    loaded: LoadedPublishJournal,
) -> dict[str, Any]:
    if loaded.phase not in {"committed", "rolled_back"}:
        raise SyncInputError("prepared publish journal cannot be finalized directly")
    _require_terminal_publish_state(paths, loaded.journal, loaded.phase)
    retained_artifact = _retire_publish_artifact_root(paths, loaded.journal)
    _cleanup_publish_journal_temps(_publish_journal_path(paths))
    history = _retire_publish_finalization_journal(paths, loaded)
    return {
        "phase": loaded.phase,
        "history": history.as_posix(),
        "retained_artifacts": (
            [retained_artifact] if retained_artifact is not None else []
        ),
    }


def _build_publish_journal(
    prepared: PreparedSync,
    artifacts: Sequence[PublishArtifact],
    committed_scan_state: Mapping[str, Any] | None,
    transaction_id: str,
    artifact_root_identity: tuple[int, int, int, int],
) -> dict[str, Any]:
    artifact_records = []
    for artifact in artifacts:
        artifact_records.append(
            {
                "kind": artifact.kind,
                "path": artifact.path.relative_to(prepared.paths.root).as_posix(),
                "before": _bytes_snapshot(artifact.original.encode("utf-8")),
                "after": _bytes_snapshot(artifact.candidate.encode("utf-8")),
            }
        )
    manifest_after = contract.stable_json_dumps(prepared.manifest).encode("utf-8")
    scan_after = (
        contract.stable_json_dumps(dict(committed_scan_state)).encode("utf-8")
        if committed_scan_state is not None
        else None
    )
    journal_path = _publish_journal_path(prepared.paths)
    return {
        "schema_version": PUBLISH_JOURNAL_SCHEMA_VERSION,
        "kind": PUBLISH_JOURNAL_KIND,
        "transaction_id": transaction_id,
        "artifact_root": _publish_artifact_root_name(journal_path, transaction_id),
        "artifact_root_identity": _serialized_identity(artifact_root_identity),
        "created_at": _utc_now(),
        "root_identity": list(prepared.paths.root_identity),
        "docs_root_identity": list(prepared.paths.docs_root_identity),
        "artifacts": artifact_records,
        "manifest": {
            "changed": prepared.manifest_changed,
            "before": _bytes_snapshot(prepared.original_manifest_bytes),
            "after": _bytes_snapshot(manifest_after),
        },
        "scan_state": {
            "needed": prepared.scan_commit_needed,
            "before": _bytes_snapshot(prepared.original_scan_state_bytes),
            "after": _bytes_snapshot(scan_after),
        },
        "manifest_generation_id": prepared.manifest["generation_id"],
        "source_snapshot_fingerprint": prepared.scan_candidate["snapshot_fingerprint"],
        "candidate_generation_id": prepared.scan_candidate["candidate_generation_id"],
    }


def _validate_publish_journal(paths: ProjectPaths, value: Any) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "transaction_id",
        "journal_identity",
        "artifact_root",
        "artifact_root_identity",
        "created_at",
        "root_identity",
        "docs_root_identity",
        "artifacts",
        "manifest",
        "scan_state",
        "manifest_generation_id",
        "source_snapshot_fingerprint",
        "candidate_generation_id",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise SyncInputError("publish recovery journal has an invalid schema")
    data = dict(value)
    if (
        data.get("schema_version") != PUBLISH_JOURNAL_SCHEMA_VERSION
        or data.get("kind") != PUBLISH_JOURNAL_KIND
    ):
        raise SyncInputError("publish recovery journal version or kind is unsupported")
    transaction_id = data.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise SyncInputError("publish recovery journal transaction_id is invalid")
    try:
        parsed_transaction_id = uuid.UUID(transaction_id)
    except ValueError as exc:
        raise SyncInputError(
            "publish recovery journal transaction_id is invalid"
        ) from exc
    if parsed_transaction_id.version != 4 or str(parsed_transaction_id) != transaction_id:
        raise SyncInputError("publish recovery journal transaction_id is not canonical UUID4")
    expected_artifact_root = _publish_artifact_root_name(
        _publish_journal_path(paths),
        transaction_id,
    )
    if data.get("artifact_root") != expected_artifact_root:
        raise SyncInputError("publish recovery journal artifact_root is invalid")
    _decode_serialized_identity(
        data.get("journal_identity"),
        "publish recovery journal journal_identity",
        expected_file_type=stat.S_IFREG,
    )
    _decode_serialized_identity(
        data.get("artifact_root_identity"),
        "publish recovery journal artifact_root_identity",
        expected_file_type=stat.S_IFDIR,
    )
    created_at = data.get("created_at")
    if not isinstance(created_at, str):
        raise SyncInputError("publish recovery journal created_at is invalid")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncInputError("publish recovery journal created_at is invalid") from exc
    if parsed_created_at.tzinfo is None:
        raise SyncInputError(
            "publish recovery journal created_at must include a timezone"
        )
    if data.get("root_identity") != list(paths.root_identity) or data.get(
        "docs_root_identity"
    ) != list(paths.docs_root_identity):
        raise SyncInputError(
            "publish recovery journal belongs to another filesystem identity"
        )
    for field in (
        "manifest_generation_id",
        "source_snapshot_fingerprint",
        "candidate_generation_id",
    ):
        digest = data.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise SyncInputError(f"publish recovery journal {field} is invalid")

    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list):
        raise SyncInputError("publish recovery journal artifacts must be a list")
    seen: set[str] = set()
    for index, item in enumerate(artifacts):
        if not isinstance(item, Mapping) or set(item) != {
            "kind",
            "path",
            "before",
            "after",
        }:
            raise SyncInputError(f"publish recovery artifact {index} is invalid")
        kind = item.get("kind")
        relative = item.get("path")
        if kind not in {"marker", "claude-index"} or not isinstance(relative, str):
            raise SyncInputError(
                f"publish recovery artifact {index} has invalid metadata"
            )
        folded_relative = relative.casefold()
        if folded_relative in seen:
            raise SyncInputError(
                f"publish recovery artifact path is duplicated: {relative}"
            )
        seen.add(folded_relative)
        if kind == "claude-index":
            if relative != "CLAUDE.md":
                raise SyncInputError("CLAUDE recovery artifact path is invalid")
        else:
            prefix = "project_docs/"
            if not relative.startswith(prefix):
                raise SyncInputError("marker recovery artifact path is invalid")
            contract.validate_manifest_relative_path(relative[len(prefix) :])
        artifact_before = _decode_bytes_snapshot(
            item.get("before"),
            f"artifact[{index}].before",
        )
        artifact_after = _decode_bytes_snapshot(
            item.get("after"),
            f"artifact[{index}].after",
        )
        if artifact_before is None:
            raise SyncInputError("publish recovery artifacts must have a preimage")
        if artifact_after is None:
            raise SyncInputError("publish recovery artifacts must have a candidate")
        if artifact_before == artifact_after:
            raise SyncInputError(
                "publish recovery artifact candidate equals its preimage"
            )

    manifest = data.get("manifest")
    if not isinstance(manifest, Mapping) or set(manifest) != {
        "changed",
        "before",
        "after",
    }:
        raise SyncInputError("publish recovery manifest snapshot is invalid")
    if type(manifest.get("changed")) is not bool:
        raise SyncInputError("publish recovery manifest changed flag is invalid")
    manifest_before = _decode_bytes_snapshot(manifest.get("before"), "manifest.before")
    manifest_after = _decode_bytes_snapshot(manifest.get("after"), "manifest.after")
    if manifest_after is None:
        raise SyncInputError("publish recovery manifest candidate is absent")
    if bool(manifest.get("changed")) != (manifest_before != manifest_after):
        raise SyncInputError("publish recovery manifest changed flag is inconsistent")
    try:
        manifest_value = contract.parse_json_strict(
            manifest_after.decode("utf-8", errors="strict"),
            source="publish recovery journal manifest.after",
        )
        validated_manifest = contract.validate_manifest_v2(
            manifest_value,
            paths.docs_root,
            verify_files=False,
            verify_complete_set=False,
        )
    except (
        UnicodeDecodeError,
        contract.ProjectDocsContractError,
    ) as exc:
        raise SyncInputError(
            f"publish recovery manifest candidate is invalid: {exc}"
        ) from exc
    if validated_manifest.get("generation_id") != data["manifest_generation_id"]:
        raise SyncInputError(
            "publish recovery manifest generation disagrees with journal metadata"
        )

    scan_state = data.get("scan_state")
    if not isinstance(scan_state, Mapping) or set(scan_state) != {
        "needed",
        "before",
        "after",
    }:
        raise SyncInputError("publish recovery scan-state snapshot is invalid")
    if type(scan_state.get("needed")) is not bool:
        raise SyncInputError("publish recovery scan-state needed flag is invalid")
    scan_before = _decode_bytes_snapshot(scan_state.get("before"), "scan_state.before")
    scan_after = _decode_bytes_snapshot(scan_state.get("after"), "scan_state.after")
    if bool(scan_state.get("needed")) != (scan_after is not None):
        raise SyncInputError("publish recovery scan-state candidate is inconsistent")
    if bool(scan_state.get("needed")) and scan_before == scan_after:
        raise SyncInputError(
            "publish recovery scan-state candidate equals its preimage"
        )
    if scan_after is not None:
        try:
            parsed_scan_after = scan_dirs.parse_previous_state_bytes(
                scan_after,
                source="publish recovery journal scan_state.after",
            )
        except (ValueError, contract.ProjectDocsContractError) as exc:
            raise SyncInputError(
                f"publish recovery scan-state candidate is invalid: {exc}"
            ) from exc
        if parsed_scan_after is None:
            raise SyncInputError("publish recovery scan-state candidate is absent")
        bindings = {
            "candidate_generation_id": data["candidate_generation_id"],
            "snapshot_fingerprint": data["source_snapshot_fingerprint"],
            "manifest_generation_id": data["manifest_generation_id"],
        }
        for field, expected_binding in bindings.items():
            if parsed_scan_after.get(field) != expected_binding:
                raise SyncInputError(
                    f"publish recovery scan-state {field} binding is inconsistent"
                )
    return data


def _load_publish_journal(paths: ProjectPaths) -> LoadedPublishJournal | None:
    slots = contract.coordinator_publish_journal_paths(paths.root)
    found: list[tuple[str, Path, bytes, tuple[int, int, int, int]]] = []
    for phase, path in slots.items():
        raw, identity = _optional_bytes_with_identity(
            path,
            label=f"{phase} publish journal",
            require_single_link=True,
        )
        if raw is not None:
            if identity is None:
                raise SyncInputError(f"{phase} publish journal has no identity")
            found.append((phase, path, raw, identity))
    if len(found) > 1:
        raise SyncInputError(
            "multiple coordinator publish journal slots are occupied: "
            f"{[phase for phase, _path, _raw, _identity in found]}"
        )
    if not found:
        pending = contract.coordinator_pending_publish_status(paths.root)
        if pending is not None:
            raise SyncInputError(
                "coordinator publish residue has no recoverable immutable journal: "
                f"{pending['entries']}"
            )
        return None

    phase, path, raw, identity = found[0]
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SyncInputError(
            f"publish recovery journal is not strict UTF-8: {exc}"
        ) from exc
    value = contract.parse_json_strict(text, source=str(path))
    journal = _validate_publish_journal(paths, value)
    if identity != _journal_expected_identity(journal):
        raise SyncInputError(
            f"{phase} publish recovery journal identity does not match its payload"
        )
    return LoadedPublishJournal(journal, raw, phase, path)


def _decode_required_text(raw: bytes | None, label: str) -> str:
    if raw is None:
        raise SyncInputError(f"{label} snapshot is unexpectedly absent")
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SyncInputError(f"{label} snapshot is not strict UTF-8") from exc


def _journal_artifacts(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> list[PublishArtifact]:
    result: list[PublishArtifact] = []
    for index, item in enumerate(journal["artifacts"]):
        relative = str(item["path"])
        kind = str(item["kind"])
        if kind == "claude-index":
            path = paths.claude_path
        else:
            docs_relative = relative[len("project_docs/") :]
            path = paths.docs_root.joinpath(*PurePosixPath(docs_relative).parts)
        before = _decode_required_text(
            _decode_bytes_snapshot(item["before"], f"artifact[{index}].before"),
            f"artifact[{index}].before",
        )
        after = _decode_required_text(
            _decode_bytes_snapshot(item["after"], f"artifact[{index}].after"),
            f"artifact[{index}].after",
        )
        result.append(PublishArtifact(path, before, after, kind))
    return result


def _restore_optional_text_snapshot(
    path: Path,
    before: bytes | None,
    after: bytes | None,
    label: str,
    paths: ProjectPaths,
    artifact_root: Path,
    artifact_root_identity: tuple[int, int, int, int],
) -> None:
    _revalidate_project_path_identities(paths)
    _set_publish_path_state(
        path,
        before,
        after,
        desired_after=False,
        allow_both=True,
        label=label,
        paths=paths,
        artifact_root=artifact_root,
        artifact_root_identity=artifact_root_identity,
    )
    _revalidate_project_path_identities(paths)


def _rollback_publish_journal(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> None:
    artifact_root = _ensure_publish_artifact_root(paths, journal)
    artifact_root_identity = _publish_artifact_root_expected_identity(journal)
    artifacts = _journal_artifacts(paths, journal)
    for artifact in reversed(artifacts):
        _set_publish_path_state(
            artifact.path,
            artifact.original.encode("utf-8"),
            artifact.candidate.encode("utf-8"),
            desired_after=False,
            allow_both=True,
            label=f"{artifact.kind} recovery rollback",
            paths=paths,
            artifact_root=artifact_root,
            artifact_root_identity=artifact_root_identity,
        )

    scan = journal["scan_state"]
    scan_before = _decode_bytes_snapshot(scan["before"], "scan_state.before")
    scan_after = _decode_bytes_snapshot(scan["after"], "scan_state.after")
    _restore_optional_text_snapshot(
        paths.scan_state_path,
        scan_before,
        scan_after,
        "scan state",
        paths,
        artifact_root,
        artifact_root_identity,
    )


def _complete_publish_journal(
    paths: ProjectPaths,
    journal: Mapping[str, Any],
) -> None:
    artifact_root = _ensure_publish_artifact_root(paths, journal)
    artifact_root_identity = _publish_artifact_root_expected_identity(journal)
    for artifact in _journal_artifacts(paths, journal):
        _require_regular_file(artifact.path, artifact.kind)
        if contract.read_utf8(artifact.path) != artifact.candidate:
            raise SyncInputError(
                f"{artifact.kind} does not contain the committed candidate during recovery"
            )

    manifest = journal["manifest"]
    manifest_after = _decode_bytes_snapshot(manifest["after"], "manifest.after")
    if _optional_bytes(paths.manifest_path) != manifest_after:
        raise SyncInputError("manifest changed during forward recovery")
    loaded_manifest = contract.load_manifest(
        paths.manifest_path,
        docs_root=paths.docs_root,
        verify_files=True,
        verify_complete_set=True,
    )
    if loaded_manifest.get("generation_id") != journal["manifest_generation_id"]:
        raise SyncInputError("manifest generation changed during forward recovery")

    fresh_source = scan_dirs.build_candidate_state(paths.root)
    if fresh_source["snapshot_fingerprint"] != journal["source_snapshot_fingerprint"]:
        raise SyncInputError("source snapshot changed before forward recovery")

    scan = journal["scan_state"]
    scan_before = _decode_bytes_snapshot(scan["before"], "scan_state.before")
    scan_after = _decode_bytes_snapshot(scan["after"], "scan_state.after")
    if scan["needed"]:
        if scan_after is None:
            raise SyncInputError("forward recovery has no scan-state candidate")
        parsed_after = scan_dirs.parse_previous_state_bytes(
            scan_after,
            source="coordinator recovery journal scan state",
        )
        if (
            parsed_after is None
            or parsed_after.get("candidate_generation_id")
            != journal["candidate_generation_id"]
        ):
            raise SyncInputError("recovery scan-state generation is invalid")
        if parsed_after.get("project_identity") != fresh_source.get("project_identity"):
            raise SyncInputError("recovery scan state belongs to another project")
        _set_publish_path_state(
            paths.scan_state_path,
            scan_before,
            scan_after,
            desired_after=True,
            allow_both=True,
            label="scan-state forward recovery",
            paths=paths,
            artifact_root=artifact_root,
            artifact_root_identity=artifact_root_identity,
        )
    elif _optional_bytes(paths.scan_state_path) != scan_before:
        raise SyncInputError("scan state changed outside the pending transaction")

    _revalidate_project_path_identities(paths)
    if _optional_bytes(paths.manifest_path) != manifest_after:
        raise SyncInputError("manifest changed before recovery completion")
    contract.load_manifest(
        paths.manifest_path,
        docs_root=paths.docs_root,
        verify_files=True,
        verify_complete_set=True,
    )


def recover_project(
    root: str | os.PathLike[str] = ".",
    *,
    lock_timeout: float = 10.0,
) -> tuple[dict[str, Any], int]:
    """Resolve one durable coordinator journal without guessing foreign changes."""

    paths = _project_paths(root)
    with _project_lock_preserving_primary(paths.root, timeout=lock_timeout):
        phase4_gate = _phase4_recovery_gate(paths.root, apply=True)
        if phase4_gate is not None:
            result = phase4_gate[0]
            result["mode"] = "recover"
            return result, EXIT_GATE_FAILED
        loaded = _load_publish_journal(paths)
        if loaded is None:
            result = _empty_error_result(
                "recover",
                "no-recovery-required",
                "no pending coordinator publish transaction exists",
            )
            result["ok"] = True
            result["status"] = "no-recovery-required"
            result["errors"] = []
            result["publish"]["error"] = None
            return result, EXIT_OK
        journal = loaded.journal
        manifest_changed = bool(journal["manifest"]["changed"])
        if loaded.phase in {"committed", "rolled_back"}:
            outcome_phase = loaded.phase
            action = f"finalized-{outcome_phase}"
            finalization = _finalize_publish_journal(paths, loaded)
        else:
            manifest = journal["manifest"]
            manifest_before = _decode_bytes_snapshot(
                manifest["before"],
                "manifest.before",
            )
            manifest_after = _decode_bytes_snapshot(
                manifest["after"],
                "manifest.after",
            )
            current_manifest = _optional_bytes(paths.manifest_path)
            manifest_changed = bool(manifest["changed"])

            artifact_root = _ensure_publish_artifact_root(paths, journal)
            artifact_root_identity = _publish_artifact_root_expected_identity(journal)
            if manifest_changed and current_manifest == manifest_after:
                _set_publish_path_state(
                    paths.manifest_path,
                    manifest_before,
                    manifest_after,
                    desired_after=True,
                    allow_both=True,
                    label="manifest forward recovery",
                    paths=paths,
                    artifact_root=artifact_root,
                    artifact_root_identity=artifact_root_identity,
                )
                _complete_publish_journal(paths, journal)
                action = "completed-forward"
                outcome_phase = "committed"
            elif current_manifest == manifest_before or (
                manifest_before is not None and current_manifest is None
            ):
                _set_publish_path_state(
                    paths.manifest_path,
                    manifest_before,
                    manifest_after,
                    desired_after=False,
                    allow_both=True,
                    label="manifest recovery rollback",
                    paths=paths,
                    artifact_root=artifact_root,
                    artifact_root_identity=artifact_root_identity,
                )
                _rollback_publish_journal(paths, journal)
                action = "rolled-back"
                outcome_phase = "rolled_back"
            else:
                raise SyncInputError(
                    "manifest is neither the recorded preimage nor committed candidate; "
                    "recovery journal was retained"
                )

            finalized = _transition_publish_journal(
                paths,
                journal,
                loaded.raw,
                outcome_phase,
            )
            finalization = _finalize_publish_journal(paths, finalized)
        result = _empty_error_result("recover", "recovered", "")
        result["ok"] = True
        result["status"] = "recovered"
        result["recovery"] = {
            "action": action,
            "manifest_generation_id": journal["manifest_generation_id"],
            "finalization": finalization,
        }
        result["errors"] = []
        committed_outcome = outcome_phase == "committed"
        result["publish"]["completed"] = committed_outcome
        result["publish"]["rolled_back"] = not committed_outcome
        result["publish"]["retained"] = copy.deepcopy(
            finalization["retained_artifacts"]
        )
        if committed_outcome:
            _set_component_disposition(
                result["publish"],
                "manifest",
                (
                    PublishDisposition.INSTALLED
                    if manifest_changed
                    else PublishDisposition.UNCHANGED
                ),
            )
            _set_component_disposition(
                result["publish"],
                "scan_state",
                (
                    PublishDisposition.INSTALLED
                    if bool(journal["scan_state"]["needed"])
                    else PublishDisposition.UNCHANGED
                ),
            )
            _set_transaction_disposition(
                result["publish"],
                PublishDisposition.INSTALLED,
            )
        else:
            _set_component_disposition(
                result["publish"],
                "manifest",
                (
                    PublishDisposition.NOT_INSTALLED
                    if manifest_changed
                    else PublishDisposition.UNCHANGED
                ),
            )
            _set_component_disposition(
                result["publish"],
                "scan_state",
                (
                    PublishDisposition.NOT_INSTALLED
                    if bool(journal["scan_state"]["needed"])
                    else PublishDisposition.UNCHANGED
                ),
            )
            _set_transaction_disposition(
                result["publish"],
                PublishDisposition.NOT_INSTALLED,
            )
        result["publish"]["error"] = None
        return result, EXIT_OK


def _write_with_detection(
    artifact: PublishArtifact,
    paths: ProjectPaths,
    artifact_root: Path,
    artifact_root_identity: tuple[int, int, int, int],
) -> BaseException | None:
    """Identity-aware CAS publication with post-install failure classification."""

    before = artifact.original.encode("utf-8")
    after = artifact.candidate.encode("utf-8")
    _revalidate_project_path_identities(paths)
    try:
        _set_publish_path_state(
            artifact.path,
            before,
            after,
            desired_after=True,
            allow_both=False,
            label=artifact.kind,
            paths=paths,
            artifact_root=artifact_root,
            artifact_root_identity=artifact_root_identity,
        )
    except BaseException as exc:
        _revalidate_project_path_identities(paths)
        if _optional_bytes(artifact.path) == after:
            return exc
        raise
    _revalidate_project_path_identities(paths)
    return None


def _rollback_artifacts(
    artifacts: Sequence[PublishArtifact],
    paths: ProjectPaths,
    artifact_root: Path,
    artifact_root_identity: tuple[int, int, int, int],
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for artifact in reversed(artifacts):
        try:
            _revalidate_project_path_identities(paths)
            _set_publish_path_state(
                artifact.path,
                artifact.original.encode("utf-8"),
                artifact.candidate.encode("utf-8"),
                desired_after=False,
                allow_both=True,
                label=f"{artifact.kind} rollback",
                paths=paths,
                artifact_root=artifact_root,
                artifact_root_identity=artifact_root_identity,
            )
        except BaseException as exc:  # pragma: no cover - double I/O failure
            failures.append(
                {
                    "path": artifact.path.as_posix(),
                    "kind": artifact.kind,
                    "error": str(exc),
                }
            )
    return failures


def _publish(
    prepared: PreparedSync, report: dict[str, Any], lock_timeout: float
) -> bool:
    publish = report["publish"]
    artifacts: list[PublishArtifact] = []
    marker_artifacts = [
        PublishArtifact(
            prepared.paths.docs_root.joinpath(*PurePosixPath(relative).parts),
            prepared.original_documents[relative],
            candidate,
            "marker",
        )
        for relative, candidate in sorted(prepared.marker_contents.items())
    ]
    if prepared.claude_changed:
        marker_artifacts.append(
            PublishArtifact(
                prepared.paths.claude_path,
                prepared.original_claude,
                prepared.claude_candidate,
                "claude-index",
            )
        )

    manifest_expected = contract.stable_json_dumps(prepared.manifest).encode("utf-8")
    try:
        committed_state = (
            _build_committed_scan_state(
                prepared.scan_candidate,
                prepared.paths.scan_state_path,
                prepared.manifest["generation_id"],
            )
            if prepared.scan_commit_needed
            else None
        )
    except BaseException as exc:
        _set_component_disposition(
            publish,
            "manifest",
            (
                PublishDisposition.NOT_INSTALLED
                if prepared.manifest_changed
                else PublishDisposition.UNCHANGED
            ),
        )
        _set_component_disposition(
            publish,
            "scan_state",
            (
                PublishDisposition.NOT_INSTALLED
                if prepared.scan_commit_needed
                else PublishDisposition.UNCHANGED
            ),
        )
        _set_transaction_disposition(publish, PublishDisposition.NOT_INSTALLED)
        publish["error"] = str(exc)
        publish["rolled_back"] = True
        raise SyncPublishError(
            f"publish transaction could not be prepared: {exc}",
            report,
        ) from exc
    expected_scan_state = (
        contract.stable_json_dumps(committed_state).encode("utf-8")
        if committed_state is not None
        else None
    )
    transaction_needed = bool(
        marker_artifacts or prepared.manifest_changed or prepared.scan_commit_needed
    )
    journal_path = _publish_journal_path(prepared.paths)
    journal_expected: bytes | None = None
    journal: dict[str, Any] | None = None
    artifact_root: Path | None = None
    artifact_root_identity: tuple[int, int, int, int] | None = None
    publication_finalized = False
    _set_component_disposition(
        publish,
        "manifest",
        (
            PublishDisposition.NOT_ATTEMPTED
            if prepared.manifest_changed
            else PublishDisposition.UNCHANGED
        ),
    )
    _set_component_disposition(
        publish,
        "scan_state",
        (
            PublishDisposition.NOT_ATTEMPTED
            if prepared.scan_commit_needed
            else PublishDisposition.UNCHANGED
        ),
    )
    _set_transaction_disposition(
        publish,
        (
            PublishDisposition.NOT_ATTEMPTED
            if transaction_needed
            else PublishDisposition.UNCHANGED
        ),
    )

    def record_published(kind: str, path: Path) -> None:
        item = {
            "kind": kind,
            "path": path.relative_to(prepared.paths.root).as_posix(),
        }
        if item not in publish["published"]:
            publish["published"].append(item)

    def classify_scan_state() -> PublishDisposition:
        try:
            current = _optional_bytes(prepared.paths.scan_state_path)
        except BaseException:
            disposition = PublishDisposition.UNKNOWN
        else:
            if expected_scan_state is not None and current == expected_scan_state:
                disposition = PublishDisposition.INSTALLED
                record_published("scan-state", prepared.paths.scan_state_path)
            elif current == prepared.original_scan_state_bytes:
                disposition = PublishDisposition.NOT_INSTALLED
            else:
                disposition = PublishDisposition.UNKNOWN
        _set_component_disposition(publish, "scan_state", disposition)
        return disposition

    try:
        with _project_lock_preserving_primary(
            prepared.paths.root,
            timeout=lock_timeout,
            secondary_errors=publish["secondary_errors"],
        ):
            recovery_gate = _phase4_recovery_gate(prepared.paths.root, apply=True)
            if recovery_gate is not None:
                report.clear()
                report.update(recovery_gate[0])
                return False
            _documents_unchanged(prepared)
            _persistent_inputs_unchanged(prepared)
            coordinator_gate = _coordinator_recovery_gate(
                prepared.paths,
                apply=True,
            )
            if coordinator_gate is not None:
                report.clear()
                report.update(coordinator_gate[0])
                return False
            if transaction_needed:
                transaction_id = str(uuid.uuid4())
                artifact_root, artifact_root_identity = _create_publish_artifact_root(
                    prepared.paths,
                    transaction_id,
                )
                journal_draft = _build_publish_journal(
                    prepared,
                    marker_artifacts,
                    committed_state,
                    transaction_id,
                    artifact_root_identity,
                )
                journal, journal_expected = _publish_journal_exclusive(
                    journal_path,
                    journal_draft,
                )
                artifact_root = _ensure_publish_artifact_root(prepared.paths, journal)
                artifact_root_identity = _publish_artifact_root_expected_identity(journal)
                _publish_fault("publish-journal-ready")

            try:
                for artifact in marker_artifacts:
                    # 先登记尝试，确保 post-replace 异常或 KeyboardInterrupt 仍会
                    # 进入精确回滚，而不会遗漏已安装但尚未记账的文件。
                    artifacts.append(artifact)
                    if artifact_root is None or artifact_root_identity is None:
                        raise SyncInputError("publish artifact root was not prepared")
                    deferred_error = _write_with_detection(
                        artifact,
                        prepared.paths,
                        artifact_root,
                        artifact_root_identity,
                    )
                    record_published(artifact.kind, artifact.path)
                    if deferred_error is not None:
                        raise deferred_error

                _documents_match_candidate(prepared)

                _revalidate_project_path_identities(prepared.paths)
                if prepared.manifest_changed:
                    if artifact_root is None or artifact_root_identity is None:
                        raise SyncInputError("publish artifact root was not prepared")
                    _set_publish_path_state(
                        prepared.paths.manifest_path,
                        prepared.original_manifest_bytes,
                        manifest_expected,
                        desired_after=True,
                        allow_both=False,
                        label="manifest",
                        paths=prepared.paths,
                        artifact_root=artifact_root,
                        artifact_root_identity=artifact_root_identity,
                    )
                elif (
                    _optional_bytes(prepared.paths.manifest_path)
                    != prepared.original_manifest_bytes
                ):
                    raise SyncInputError("manifest preimage changed before commit")
                _revalidate_project_path_identities(prepared.paths)
                _verify_committed_manifest(prepared)
                if prepared.manifest_changed:
                    _set_component_disposition(
                        publish,
                        "manifest",
                        PublishDisposition.INSTALLED,
                    )
                    record_published("manifest", prepared.paths.manifest_path)
                _publish_fault("manifest-verified")
            except BaseException as exc:
                # 覆盖 atomic replace 成功后、Python 尚未来得及记录处置状态
                # 就收到中断的窗口。未知字节绝不能按 pre-commit 回滚处理。
                manifest_disposition = PublishDisposition(
                    publish["manifest_disposition"]
                )
                if (
                    prepared.manifest_changed
                    and manifest_disposition is not PublishDisposition.INSTALLED
                ):
                    try:
                        current_manifest = _optional_bytes(prepared.paths.manifest_path)
                    except BaseException:
                        manifest_disposition = PublishDisposition.UNKNOWN
                    else:
                        if current_manifest == manifest_expected:
                            manifest_disposition = PublishDisposition.INSTALLED
                        elif current_manifest == prepared.original_manifest_bytes:
                            manifest_disposition = PublishDisposition.NOT_INSTALLED
                        else:
                            manifest_disposition = PublishDisposition.UNKNOWN
                    _set_component_disposition(
                        publish,
                        "manifest",
                        manifest_disposition,
                    )

                if manifest_disposition in {
                    PublishDisposition.INSTALLED,
                    PublishDisposition.UNKNOWN,
                }:
                    _set_transaction_disposition(
                        publish,
                        (
                            PublishDisposition.PARTIALLY_INSTALLED
                            if manifest_disposition is PublishDisposition.INSTALLED
                            else PublishDisposition.UNKNOWN
                        ),
                    )
                    publish["error"] = str(exc)
                    raise SyncPublishError(
                        f"publish failed at or after manifest commit: {exc}",
                        report,
                    ) from exc

                rollback_failures = (
                    [
                        {
                            "path": journal_path.as_posix(),
                            "kind": "publish-artifacts",
                            "error": "publish artifact root is unavailable for rollback",
                        }
                    ]
                    if artifact_root is None or artifact_root_identity is None
                    else _rollback_artifacts(
                        artifacts,
                        prepared.paths,
                        artifact_root,
                        artifact_root_identity,
                    )
                )
                rollback_state_complete = not rollback_failures
                if rollback_state_complete and journal_expected is not None:
                    try:
                        if journal is None:
                            raise SyncInputError(
                                "publish journal payload is unavailable during rollback"
                            )
                        rolled_back_journal = _transition_publish_journal(
                            prepared.paths,
                            journal,
                            journal_expected,
                            "rolled_back",
                        )
                        finalization = _finalize_publish_journal(
                            prepared.paths,
                            rolled_back_journal,
                        )
                        publish["retained"].extend(
                            copy.deepcopy(finalization["retained_artifacts"])
                        )
                    except BaseException as journal_exc:
                        publish["finalization_pending"] = True
                        publish["secondary_errors"].append(
                            {
                                "stage": "rollback-finalization",
                                "error": str(journal_exc),
                            }
                        )
                publish["rolled_back"] = rollback_state_complete
                publish["rollback_failures"] = rollback_failures
                _set_transaction_disposition(
                    publish,
                    (
                        PublishDisposition.NOT_INSTALLED
                        if rollback_state_complete
                        else PublishDisposition.UNKNOWN
                    ),
                )
                publish["error"] = str(exc)
                raise SyncPublishError(
                    f"publish failed before manifest commit: {exc}",
                    report,
                ) from exc

            if prepared.scan_commit_needed:
                try:
                    if committed_state is None or expected_scan_state is None:
                        raise SyncInputError("prepared scan-state candidate is missing")
                    if (
                        _optional_bytes(prepared.paths.scan_state_path)
                        != prepared.original_scan_state_bytes
                    ):
                        raise SyncInputError(
                            "scan state changed before candidate promotion"
                        )
                    _revalidate_project_path_identities(prepared.paths)
                    _documents_match_candidate(prepared)
                    _verify_committed_manifest(prepared)
                    _source_snapshot_unchanged(prepared)
                    if artifact_root is None or artifact_root_identity is None:
                        raise SyncInputError("publish artifact root was not prepared")
                    _set_publish_path_state(
                        prepared.paths.scan_state_path,
                        prepared.original_scan_state_bytes,
                        expected_scan_state,
                        desired_after=True,
                        allow_both=False,
                        label="scan state",
                        paths=prepared.paths,
                        artifact_root=artifact_root,
                        artifact_root_identity=artifact_root_identity,
                    )
                    _revalidate_project_path_identities(prepared.paths)
                    _documents_match_candidate(prepared)
                    _verify_committed_manifest(prepared)
                    _source_snapshot_unchanged(prepared)
                    _publish_fault("scan-state-verified")
                    _set_component_disposition(
                        publish,
                        "scan_state",
                        PublishDisposition.INSTALLED,
                    )
                    record_published("scan-state", prepared.paths.scan_state_path)
                    _publish_fault("scan-state-recorded")
                except BaseException as exc:
                    scan_disposition = classify_scan_state()
                    manifest_disposition = PublishDisposition(
                        publish["manifest_disposition"]
                    )
                    if scan_disposition is PublishDisposition.UNKNOWN:
                        transaction_disposition = PublishDisposition.UNKNOWN
                    elif (
                        scan_disposition is PublishDisposition.NOT_INSTALLED
                        and manifest_disposition is PublishDisposition.UNCHANGED
                        and not artifacts
                    ):
                        transaction_disposition = PublishDisposition.NOT_INSTALLED
                    else:
                        transaction_disposition = PublishDisposition.PARTIALLY_INSTALLED
                    _set_transaction_disposition(
                        publish,
                        transaction_disposition,
                    )
                    failure_message = (
                        "scan state publish failed "
                        f"(manifest={manifest_disposition.value}, "
                        f"installed_artifacts={len(artifacts)}, "
                        f"scan_state={scan_disposition.value}): {exc}"
                    )
                    publish["error"] = failure_message
                    raise SyncPublishError(failure_message, report) from exc

            _publish_fault("publish-before-journal-cleanup")
            if journal_expected is not None:
                if journal is None:
                    raise SyncInputError(
                        "publish journal payload is unavailable during commit"
                    )
                try:
                    committed_journal = _transition_publish_journal(
                        prepared.paths,
                        journal,
                        journal_expected,
                        "committed",
                    )
                except BaseException as exc:
                    pending = _load_publish_journal(prepared.paths)
                    if pending is not None and pending.phase == "committed":
                        _set_transaction_disposition(
                            publish,
                            PublishDisposition.INSTALLED,
                        )
                        publish["finalization_pending"] = True
                        publish["error"] = str(exc)
                        raise SyncPublishError(
                            f"publication committed but journal finalization is pending: {exc}",
                            report,
                        ) from exc
                    raise
                _set_transaction_disposition(
                    publish,
                    PublishDisposition.INSTALLED,
                )
                try:
                    finalization = _finalize_publish_journal(
                        prepared.paths,
                        committed_journal,
                    )
                    publish["retained"].extend(
                        copy.deepcopy(finalization["retained_artifacts"])
                    )
                except BaseException as exc:
                    publish["finalization_pending"] = True
                    publish["error"] = str(exc)
                    raise SyncPublishError(
                        f"publication committed but cleanup finalization is pending: {exc}",
                        report,
                    ) from exc
            else:
                _set_transaction_disposition(
                    publish,
                    PublishDisposition.UNCHANGED,
                )
            publication_finalized = True
            _publish_fault("publish-completed")
        publish["completed"] = True
    except SyncPublishError:
        raise
    except BaseException as exc:
        if publication_finalized:
            publish["completed"] = False
            publish["error"] = str(exc)
            raise SyncPublishError(
                f"publication was finalized but lock release failed: {exc}",
                report,
            ) from exc

        manifest_disposition = PublishDisposition(publish["manifest_disposition"])
        scan_disposition = PublishDisposition(publish["scan_state_disposition"])
        if (
            prepared.scan_commit_needed
            and scan_disposition is PublishDisposition.NOT_ATTEMPTED
        ):
            scan_disposition = classify_scan_state()
        if any(
            disposition
            in {
                PublishDisposition.INSTALLED,
                PublishDisposition.UNKNOWN,
            }
            for disposition in (manifest_disposition, scan_disposition)
        ):
            if PublishDisposition.UNKNOWN in {
                manifest_disposition,
                scan_disposition,
            }:
                _set_transaction_disposition(
                    publish,
                    PublishDisposition.UNKNOWN,
                )
            else:
                _set_transaction_disposition(
                    publish,
                    PublishDisposition.PARTIALLY_INSTALLED,
                )
            publish["completed"] = False
            failure_message = (
                "publish failed with unresolved durable state "
                f"(manifest={manifest_disposition.value}, "
                f"scan_state={scan_disposition.value}, "
                f"installed_artifacts={len(artifacts)}): {exc}"
            )
            publish["error"] = failure_message
            raise SyncPublishError(failure_message, report) from exc
        raise
    return True


def _prepare(
    paths: ProjectPaths,
    stage_docs: Path,
    *,
    run_timestamp: str,
    force_full: bool,
) -> PreparedSync:
    # 先捕获所有持久输入预像，再基于这些输入作候选决策。即使后续
    # 解析阶段观察到并发替换，发布锁内的精确字节复核也会拒绝提交。
    original_claude = contract.read_utf8(paths.claude_path)
    original_manifest_bytes = _optional_bytes(paths.manifest_path)
    original_scan_state_bytes = _optional_bytes(paths.scan_state_path)

    marker_contents, marker_originals, marker_details = _normalized_marker_files(
        paths.docs_root
    )
    captured_docs = stage_docs.parent / ".captured-project-docs"
    captured_docs.mkdir()
    original_documents = _stage_documents(
        paths.docs_root,
        stage_docs,
        captured_docs,
        marker_contents,
        marker_originals,
    )
    current_manifest, manifest_warnings = _load_current_manifest(
        paths,
        original_manifest_bytes,
        captured_docs,
    )
    manifest, manifest_changed, current_generation, current_schema = _select_manifest(
        paths,
        stage_docs,
        run_timestamp,
        current_manifest,
        original_manifest_bytes,
    )
    previous_state, scan_candidate, scan_commit_needed = _scan_candidate(
        paths,
        original_scan_state_bytes,
        force_full=force_full,
        manifest_generation_id=manifest["generation_id"],
    )
    source_snapshot_root = stage_docs.parent
    known_source_paths = _stage_source_snapshot(
        source_snapshot_root,
        scan_candidate,
    )
    source_inventory = _phase4_source_inventory(scan_candidate)
    validation, validation_errors = _validate_documents(
        source_snapshot_root,
        known_source_paths,
        stage_docs,
        manifest,
        scan_candidate,
    )
    broken_count, broken = link_check.check_links(stage_docs)
    links = {"ok": broken_count == 0, "broken_count": broken_count, "broken": broken}
    link_errors = (
        [
            {
                "code": "broken-document-links",
                "message": f"candidate document tree contains {broken_count} invalid or missing links",
            }
        ]
        if broken_count
        else []
    )

    phase4, phase4_errors, phase4_warnings = _phase4_report(
        source_snapshot_root,
        stage_docs,
        manifest,
        source_inventory,
    )
    phase4_counts = phase4.get("counts", {})
    phase4_candidates = (
        phase4_counts.get("candidates", 0) if isinstance(phase4_counts, Mapping) else 0
    )
    if type(phase4_candidates) is int and phase4_candidates > 0 and scan_commit_needed:
        # Quarantine candidates freeze the scan baseline only.  This keeps the
        # reference-clearing edits publishable so the quarantine workflow can
        # complete instead of deadlocking between the coordinator gate and the
        # Phase 4 manifest consistency check.
        scan_commit_needed = False
        phase4_warnings.append(
            {
                "code": "scan-baseline-frozen",
                "severity": "warning",
                "message": (
                    "scan baseline commit deferred until pending Phase 4 "
                    "candidates are quarantined"
                ),
            }
        )
    claude_candidate = claude_doc_index.replace_doc_tree_section(
        original_claude,
        manifest,
    )
    claude_changed = claude_candidate != original_claude

    return PreparedSync(
        paths=paths,
        stage_docs=stage_docs,
        run_timestamp=run_timestamp,
        force_full=force_full,
        original_documents=original_documents,
        marker_contents=marker_contents,
        marker_details=marker_details,
        original_claude=original_claude,
        original_manifest_bytes=original_manifest_bytes,
        original_scan_state_bytes=original_scan_state_bytes,
        previous_scan_state=previous_state,
        scan_candidate=scan_candidate,
        scan_commit_needed=scan_commit_needed,
        manifest=manifest,
        manifest_changed=manifest_changed,
        current_manifest_generation=current_generation,
        current_manifest_schema=current_schema,
        claude_candidate=claude_candidate,
        claude_changed=claude_changed,
        validation=validation,
        links=links,
        phase4=phase4,
        warnings=[*manifest_warnings, *phase4_warnings],
        errors=[*validation_errors, *link_errors, *phase4_errors],
    )


def _build_worklist_scaffold(prepared: PreparedSync) -> list[dict[str, Any]]:
    docs_root = prepared.paths.docs_root
    directories = prepared.scan_candidate.get("directories", [])
    scaffold: list[dict[str, Any]] = []

    for item in directories:
        path = str(item.get("path", ""))
        change = item.get("change", "unchanged")
        snapshot = item.get("snapshot", {})
        files = snapshot.get("files", [])
        child_dirs = snapshot.get("child_directories", [])
        is_empty = bool(item.get("is_empty", False))

        recommended_profile = "placeholder" if is_empty else "standard"
        doc_relative = "index.md" if not path else f"{path}.md"
        doc_project_path = (
            "project_docs/index.md" if not path else f"project_docs/{path}.md"
        )

        precomputed_links: dict[str, Any] = {
            "children": [],
        }

        # Parent doc link
        parent_path = posixpath.dirname(path) if path else ""
        if parent_path:
            parent_doc = f"{parent_path}.md"
            try:
                link = contract.compute_relative_doc_link(
                    doc_relative, parent_doc, docs_root
                )
                precomputed_links["parent"] = {
                    "path": parent_path,
                    "doc_path": f"project_docs/{parent_path}.md",
                    "relative_link": link,
                }
            except Exception:
                pass

        # Children doc links
        for child in child_dirs:
            child_subpath = child if not path else f"{path}/{child}"
            child_doc = f"{child_subpath}.md"
            try:
                link = contract.compute_relative_doc_link(
                    doc_relative, child_doc, docs_root
                )
                precomputed_links["children"].append(
                    {
                        "name": child,
                        "path": child_subpath,
                        "doc_path": f"project_docs/{child_subpath}.md",
                        "relative_link": link,
                    }
                )
            except Exception:
                pass

        # Tests doc link
        try:
            tests_link = contract.compute_relative_doc_link(
                doc_relative, "tests.md", docs_root
            )
            precomputed_links["tests"] = {
                "doc_path": "project_docs/tests.md",
                "relative_link": tests_link,
            }
        except Exception:
            pass

        direct_files = [
            {
                "name": f.get("name"),
                "size": f.get("size"),
                "is_py": str(f.get("name", "")).casefold().endswith(".py"),
            }
            for f in files
        ]

        scaffold.append(
            {
                "path": path,
                "change": change,
                "doc_path": doc_project_path,
                "recommended_profile": recommended_profile,
                "direct_files": direct_files,
                "direct_subdirectories": list(child_dirs),
                "precomputed_links": precomputed_links,
            }
        )

    return scaffold


def _report_warnings(prepared: PreparedSync) -> list[Any]:
    """Prepared warnings plus any runtime durability degradation observed."""

    warnings_value = [copy.deepcopy(item) for item in prepared.warnings]
    degraded = phase4_exit.directory_fsync_degraded()
    if degraded:
        warnings_value.append(
            {
                "code": "durability-degraded",
                "severity": "warning",
                "message": (
                    "directory fsync was skipped because this filesystem refuses it; "
                    "crash durability of the published namespace is degraded: "
                    + "; ".join(degraded)
                ),
            }
        )
    return warnings_value


def _report(prepared: PreparedSync, *, apply: bool) -> dict[str, Any]:
    scan = prepared.scan_candidate
    planned: list[dict[str, str]] = [
        {"kind": "marker", "path": path} for path in sorted(prepared.marker_contents)
    ]
    if prepared.claude_changed:
        planned.append({"kind": "claude-index", "path": "CLAUDE.md"})
    if prepared.manifest_changed:
        planned.append({"kind": "manifest", "path": "project_docs/_manifest.json"})
    if prepared.scan_commit_needed:
        planned.append({"kind": "scan-state", "path": "project_docs/_scan_state.json"})

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": prepared.gates_passed,
        "mode": "apply" if apply else "dry-run",
        "root": prepared.paths.root.as_posix(),
        "run_timestamp": prepared.run_timestamp,
        "scan": {
            "mode": scan["mode"],
            "baseline": copy.deepcopy(scan["baseline"]),
            "candidate_generation_id": scan["candidate_generation_id"],
            "snapshot_fingerprint": scan["snapshot_fingerprint"],
            "changes": copy.deepcopy(scan["changes"]),
            "change_counts": copy.deepcopy(scan["change_counts"]),
            "worklist": {
                "directories": copy.deepcopy(scan["directories"]),
                "deleted_directories": copy.deepcopy(scan["deleted_directories"]),
                "unknown_directories": copy.deepcopy(scan["unknown_directories"]),
                "scaffold": _build_worklist_scaffold(prepared),
            },
            "full_total": scan["full_total"],
            "boundary_count": scan["boundary_count"],
            "skipped_directory_count": scan["skipped_directory_count"],
            "unknown_scopes": copy.deepcopy(scan["snapshot"]["unknown_scopes"]),
            "commit_needed": prepared.scan_commit_needed,
        },
        "markers": {
            "candidate_count": len(prepared.marker_details),
            "candidate_file_count": len(prepared.marker_contents),
            "changes": prepared.marker_details,
        },
        "validation": prepared.validation,
        "links": prepared.links,
        "phase4": prepared.phase4,
        "manifest": {
            "schema_version": prepared.manifest["schema_version"],
            "generation_id": prepared.manifest["generation_id"],
            "current_generation_id": prepared.current_manifest_generation,
            "current_schema_version": prepared.current_manifest_schema,
            "changed": prepared.manifest_changed,
            "total": prepared.manifest["total"],
            "depth_counts": prepared.manifest["depth_counts"],
        },
        "claude": {"changed": prepared.claude_changed},
        "planned_writes": planned,
        "warnings": _report_warnings(prepared),
        "errors": prepared.errors,
        "publish": {
            "requested": apply,
            "completed": False,
            "disposition": PublishDisposition.NOT_ATTEMPTED.value,
            "manifest_disposition": PublishDisposition.NOT_ATTEMPTED.value,
            "scan_state_disposition": PublishDisposition.NOT_ATTEMPTED.value,
            "manifest_commit_point": False,
            "manifest_commit_ambiguous": False,
            "scan_state_committed": False,
            "scan_state_commit_ambiguous": False,
            "partial_commit": False,
            "rolled_back": False,
            "rollback_failures": [],
            "secondary_errors": [],
            "finalization_pending": False,
            "retained": [],
            "published": [],
            "error": None,
        },
    }


def _create_stage_root(paths: ProjectPaths) -> Path:
    """Create coordinator staging under the configured system temp directory."""

    try:
        system_temp = Path(tempfile.gettempdir()).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SyncInputError(
            f"system temporary directory is unavailable: {exc}"
        ) from exc
    if system_temp == paths.root or system_temp.is_relative_to(paths.root):
        raise SyncInputError(
            "system temporary directory must be outside the project tree"
        )
    try:
        created = Path(
            tempfile.mkdtemp(
                prefix="proj-doc-builder-stage-",
                dir=system_temp,
            )
        )
    except OSError as exc:
        raise SyncInputError(
            f"cannot create coordinator staging directory: {exc}"
        ) from exc
    try:
        stage_root = created.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        try:
            shutil.rmtree(created)
        except OSError as cleanup_exc:
            raise SyncInputError(
                "cannot resolve or clean coordinator staging directory: "
                f"resolve={exc}; cleanup={cleanup_exc}"
            ) from cleanup_exc
        raise SyncInputError(
            f"cannot resolve coordinator staging directory: {exc}"
        ) from exc
    if stage_root == paths.root or stage_root.is_relative_to(paths.root):
        try:
            shutil.rmtree(stage_root)
        except OSError as cleanup_exc:
            raise SyncInputError(
                "coordinator staging was created inside the project tree and "
                f"cleanup failed: {cleanup_exc}"
            ) from cleanup_exc
        raise SyncInputError("coordinator staging must not be inside the project tree")
    return stage_root


def _remove_stage_root(
    stage_root: Path,
    expected_identity: tuple[int, int, int, int],
) -> None:
    """Detach only the staging directory created by this invocation, then remove it."""

    if _is_reparse(stage_root):
        raise SyncInputError(
            f"coordinator staging directory became indirect: {stage_root}"
        )
    try:
        current = stage_root.lstat()
    except OSError as exc:
        raise SyncInputError(
            f"cannot inspect coordinator staging directory {stage_root}: {exc}"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode)
        or phase4_exit._identity_from_stat(current) != expected_identity
    ):
        raise SyncInputError(
            f"coordinator staging directory identity changed: {stage_root}"
        )

    detached = stage_root.with_name(
        f".{stage_root.name}.cleanup-{uuid.uuid4().hex}"
    )
    try:
        phase4_exit._rename_no_replace(stage_root, detached)
        detached_state = detached.lstat()
    except OSError as exc:
        raise SyncInputError(
            f"cannot detach coordinator staging directory {stage_root}: {exc}"
        ) from exc
    if (
        os.path.lexists(stage_root)
        or _is_reparse(detached)
        or not stat.S_ISDIR(detached_state.st_mode)
        or phase4_exit._identity_from_stat(detached_state) != expected_identity
    ):
        raise SyncInputError(
            f"coordinator staging identity changed during detachment: {stage_root}"
        )
    _fsync_parent_directory(detached.parent)
    try:
        shutil.rmtree(detached)
    except OSError as exc:
        raise SyncInputError(
            f"cannot clean coordinator staging directory {detached}: {exc}"
        ) from exc
    if os.path.lexists(detached):
        raise SyncInputError(
            f"detached coordinator staging directory remains: {detached}"
        )
    _fsync_parent_directory(detached.parent)


@contextmanager
def _coordinator_stage(paths: ProjectPaths) -> Any:
    stage_root = _create_stage_root(paths)
    try:
        identity = phase4_exit._identity_from_stat(stage_root.lstat())
    except OSError as exc:
        raise SyncInputError(
            f"cannot capture coordinator staging identity: {exc}"
        ) from exc
    try:
        yield stage_root
    except BaseException as primary:
        try:
            _remove_stage_root(stage_root, identity)
        except BaseException as cleanup_exc:
            if isinstance(primary, SyncPublishError):
                primary.result["publish"].setdefault("secondary_errors", []).append(
                    {
                        "stage": "staging-cleanup",
                        "error": str(cleanup_exc),
                    }
                )
            add_note = getattr(primary, "add_note", None)
            if callable(add_note):
                add_note(f"coordinator staging cleanup also failed: {cleanup_exc}")
        raise
    else:
        _remove_stage_root(stage_root, identity)


def synchronize_project(
    root: str | os.PathLike[str] = ".",
    *,
    apply: bool = False,
    force_full: bool = False,
    lock_timeout: float = 10.0,
    run_timestamp: str | None = None,
) -> tuple[dict[str, Any], int]:
    """Run all gates and optionally publish the prepared transaction."""

    paths = _project_paths(root)
    recovery_gate = _coordinator_recovery_gate(paths, apply=apply)
    if recovery_gate is not None:
        return recovery_gate
    recovery_gate = _phase4_recovery_gate(paths.root, apply=apply)
    if recovery_gate is not None:
        return recovery_gate
    timestamp = run_timestamp or _utc_now()
    # Reuse the manifest parser for strict timezone validation before staging.
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SyncInputError(
            f"run_timestamp is not valid ISO-8601: {timestamp!r}"
        ) from exc

    with _coordinator_stage(paths) as stage_root:
        stage_docs = stage_root / "project_docs"
        stage_docs.mkdir()
        prepared = _prepare(
            paths,
            stage_docs,
            run_timestamp=timestamp,
            force_full=force_full,
        )
        recovery_gate = _coordinator_recovery_gate(paths, apply=apply)
        if recovery_gate is not None:
            return recovery_gate
        recovery_gate = _phase4_recovery_gate(paths.root, apply=apply)
        if recovery_gate is not None:
            return recovery_gate
        report = _report(prepared, apply=apply)
        if not prepared.gates_passed:
            report["ok"] = False
            return report, EXIT_GATE_FAILED
        if not apply:
            report["ok"] = True
            return report, EXIT_OK

        # 即使零写入也必须进入统一根锁并执行锁内 Phase 4 recovery、
        # manifest 和活动 inventory 复核，不能用无锁快速路径伪造完成状态。
        if not _publish(prepared, report, lock_timeout):
            report["ok"] = False
            return report, EXIT_GATE_FAILED
        report["ok"] = True
        return report, EXIT_OK


def _coordinator_recovery_gate(
    paths: ProjectPaths,
    *,
    apply: bool,
) -> tuple[dict[str, Any], int] | None:
    mode = "apply" if apply else "dry-run"
    try:
        pending = contract.coordinator_pending_publish_status(paths.root)
        if pending is None:
            return None
        loaded = _load_publish_journal(paths)
    except (
        SyncInputError,
        contract.ProjectDocsContractError,
    ) as exc:
        recovery = {"required": True, "detail": str(exc)}
    else:
        if loaded is None:
            recovery = copy.deepcopy(pending)
            recovery["detail"] = (
                "coordinator journal residue exists without a prepared journal"
            )
        else:
            journal = loaded.journal
            recovery = {
                "required": True,
                "kind": journal["kind"],
                "phase": loaded.phase,
                "created_at": journal["created_at"],
                "manifest_generation_id": journal["manifest_generation_id"],
                "transaction_id": journal["transaction_id"],
                "entries": copy.deepcopy(pending["entries"]),
            }
    message = "a pending coordinator publish transaction requires --recover"
    if isinstance(recovery.get("detail"), str):
        message = f"{message}: {recovery['detail']}"
    result = _empty_error_result(mode, "coordinator-recovery-required", message)
    result["status"] = "coordinator-recovery-required"
    result["recovery"] = recovery
    result["planned_writes"] = []
    return result, EXIT_GATE_FAILED


def _phase4_recovery_gate(
    root: Path,
    *,
    apply: bool,
) -> tuple[dict[str, Any], int] | None:
    """Block coordinator staging/publish while a Phase 4 journal is unresolved."""

    mode = "apply" if apply else "dry-run"
    try:
        recovery = phase4_exit.pending_recovery_status(root)
    except phase4_exit.Phase4Refusal as exc:
        recovery = {"required": True, "detail": exc.message}
    if recovery is None:
        return None
    message = "a pending Phase 4 transaction requires phase4_exit.py --recover"
    if isinstance(recovery.get("detail"), str):
        message = f"{message}: {recovery['detail']}"
    result = _empty_error_result(mode, "phase4-recovery-required", message)
    result["status"] = "phase4-recovery-required"
    result["phase4"] = {
        "status": "phase4-recovery-required",
        "recovery": recovery,
    }
    result["scan"] = {
        "commit_needed": False,
        "blocked_by_phase4_recovery": True,
    }
    result["planned_writes"] = []
    return result, EXIT_GATE_FAILED


def _empty_error_result(mode: str, code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "ok": False,
        "mode": mode,
        "errors": [{"code": code, "message": message}],
        "publish": {
            "requested": mode == "apply",
            "completed": False,
            "disposition": PublishDisposition.NOT_ATTEMPTED.value,
            "manifest_disposition": PublishDisposition.NOT_ATTEMPTED.value,
            "scan_state_disposition": PublishDisposition.NOT_ATTEMPTED.value,
            "manifest_commit_point": False,
            "manifest_commit_ambiguous": False,
            "scan_state_committed": False,
            "scan_state_commit_ambiguous": False,
            "partial_commit": False,
            "rolled_back": False,
            "rollback_failures": [],
            "secondary_errors": [],
            "finalization_pending": False,
            "retained": [],
            "published": [],
            "error": message,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage, validate, and atomically finalize project_docs synchronization"
    )
    parser.add_argument("root", nargs="?", default=".", help="project root")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="publish after all gates pass"
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit report-only mode (also the default)",
    )
    mode.add_argument(
        "--recover",
        action="store_true",
        help="recover one pending coordinator publish transaction",
    )
    parser.add_argument(
        "--full", action="store_true", help="select all directories for audit"
    )
    parser.add_argument("--lock-timeout", type=float, default=10.0)
    parser.add_argument(
        "--json", action="store_true", help="emit exactly one JSON document"
    )
    return parser


def _emit(result: Mapping[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(contract.stable_json_dumps(dict(result)))
        return
    status = "通过" if result.get("ok") else "失败"
    print(f"proj-doc-builder {result.get('mode', 'unknown')}: {status}")
    manifest = result.get("manifest")
    if isinstance(manifest, Mapping):
        print(
            f"文档: {manifest.get('total')}，generation: "
            f"{manifest.get('generation_id')}"
        )
    for error in result.get("errors", []):
        if isinstance(error, Mapping):
            print(
                f"ERROR [{error.get('code')}]: {error.get('message')}", file=sys.stderr
            )


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = _parser().parse_args(argv)
    mode = "recover" if args.recover else "apply" if args.apply else "dry-run"
    try:
        if args.lock_timeout < 0:
            raise SyncInputError("--lock-timeout must be non-negative")
        if args.recover:
            if args.full:
                raise SyncInputError("--full cannot be combined with --recover")
            result, exit_code = recover_project(
                args.root,
                lock_timeout=args.lock_timeout,
            )
        else:
            result, exit_code = synchronize_project(
                args.root,
                apply=args.apply,
                force_full=args.full,
                lock_timeout=args.lock_timeout,
            )
    except SyncPublishError as exc:
        result = exc.result
        result["ok"] = False
        result["errors"].append({"code": "publish-failed", "message": str(exc)})
        exit_code = EXIT_PUBLISH_FAILED
    except (
        OSError,
        RuntimeError,
        ValueError,
        contract.ProjectDocsContractError,
    ) as exc:
        result = _empty_error_result(mode, "input-or-operational-error", str(exc))
        exit_code = EXIT_INPUT_ERROR
    result["exit_code"] = exit_code
    _emit(result, as_json=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
