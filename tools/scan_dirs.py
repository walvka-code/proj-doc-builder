# -*- coding: utf-8 -*-
"""Safe, fingerprint-based Phase 1 directory scanner.

A scan produces a schema-v2 *candidate* state.  The committed baseline is only
replaced by :func:`commit_candidate_state` (or ``--commit``), after the caller
has successfully processed the candidate.  This prevents a failed docs sync
from losing the previous incremental baseline.

The legacy command-line flags remain accepted.  New callers should pass
``--state`` for the committed baseline and ``--candidate`` (or ``--output``)
for the candidate file.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import hmac
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

import project_docs_contract as contract

EXCLUDE_NAMES = {
    ".git",
    ".svn",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
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
    ".hypothesis",
}

MAX_DEPTH = 8
SCAN_STATE_SCHEMA_VERSION = 2
SCAN_STATE_KIND = "sync-project-docs-directory-scan"
SCAN_STATE_PRODUCER = {"name": "sync-project-docs.scan_dirs", "version": "2.1"}
DEFAULT_STATE_RELATIVE_PATH = Path("project_docs") / "_scan_state.json"
_COMMIT_GENERATED = "__generated_candidate__"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ROOT = "."
_PROJECT_ID_DOMAIN = b"sync-project-docs-project-root-v1\0"
_STATE_LOCK_DOMAIN = b"sync-project-docs-scan-state-target-v1\0"
_FILE_HASH_CHUNK_SIZE = 1024 * 1024
_STABLE_SCAN_MAX_PASSES = 4
_EXCLUDE_NAMES_CASEFOLD = frozenset(name.casefold() for name in EXCLUDE_NAMES)


class ScanError(RuntimeError):
    """A directory tree cannot be scanned without an unsafe or partial result."""


class ScanStateError(ValueError):
    """A persisted scan state does not satisfy the scanner contract."""


class BaselineConflict(ScanStateError):
    """The committed baseline no longer matches the candidate precondition."""


class CommitDisposition(str, Enum):
    """Observable result after a candidate commit operation raises."""

    INSTALLED = "installed"
    NOT_INSTALLED = "not-installed"
    UNKNOWN = "unknown"


class CandidateCommitError(ScanStateError):
    """Candidate publication failed with an explicit installed-state verdict."""

    def __init__(
        self,
        message: str,
        *,
        disposition: CommitDisposition,
        durability_confirmed: bool,
    ) -> None:
        super().__init__(message)
        self.disposition = disposition
        self.durability_confirmed = durability_confirmed


@dataclass(frozen=True)
class BaselinePrecondition:
    """Exact committed-state preimage used to classify a candidate."""

    exists: bool
    sha256: str | None
    generation_id: str | None


def should_prune(name: str) -> bool:
    """Return whether a directory name is outside the documentation scan."""

    return name.casefold() in _EXCLUDE_NAMES_CASEFOLD or name.startswith(".")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolved_root(root: str | os.PathLike[str]) -> Path:
    candidate = Path(root)
    try:
        lexical = Path(os.path.abspath(candidate))
        lexical_state = lexical.lstat()
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanError(
            f"root directory cannot be resolved: {candidate}: {exc}"
        ) from exc
    if (
        lexical.is_symlink()
        or _is_reparse(lexical_state)
        or os.path.normcase(os.path.normpath(str(lexical)))
        != os.path.normcase(os.path.normpath(str(resolved)))
    ):
        raise ScanError(f"root directory must not use an indirect path: {candidate}")
    if not stat.S_ISDIR(lexical_state.st_mode) or not resolved.is_dir():
        raise ScanError(f"root path is not a directory: {resolved}")
    return resolved


def _validate_relative_directory_path(relative_path: str) -> str:
    if relative_path == "":
        return ""
    # Reuse the shared portable path contract by validating a synthetic document
    # below the directory.  This rejects absolute, drive-qualified, traversal,
    # backslash, empty-segment, and NUL-containing paths consistently.
    try:
        contract.validate_manifest_relative_path(
            f"{relative_path}/__scan_directory__.md"
        )
    except contract.PathValidationError as exc:
        raise ScanStateError(
            f"invalid scan directory path {relative_path!r}: {exc}"
        ) from exc
    return relative_path


def _join_relative(parent: str, name: str) -> str:
    if not name or "/" in name or "\\" in name or "\0" in name:
        raise ScanError(f"directory entry has a non-portable name: {name!r}")
    relative = f"{parent}/{name}" if parent else name
    return _validate_relative_directory_path(relative)


def _is_within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _project_identity_from_path(root: Path) -> str:
    """Return an opaque, stable identity without persisting the absolute root."""

    normalized = os.path.normpath(str(root.resolve(strict=False)))
    if os.name == "nt":
        normalized = normalized.replace("\\", "/").casefold()
    return hashlib.sha256(
        _PROJECT_ID_DOMAIN + normalized.encode("utf-8", errors="strict")
    ).hexdigest()


def _project_identity(root: Path) -> str:
    return _project_identity_from_path(root.resolve(strict=True))


def _mtime_ns(stat_result: os.stat_result) -> int:
    return getattr(
        stat_result,
        "st_mtime_ns",
        int(stat_result.st_mtime * 1_000_000_000),
    )


def _stat_identity(stat_result: os.stat_result) -> tuple[int, int]:
    return (int(stat_result.st_dev), int(stat_result.st_ino))


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    first_id = _stat_identity(first)
    second_id = _stat_identity(second)
    if first_id != (0, 0) and second_id != (0, 0):
        return first_id == second_id
    return (
        stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and first.st_size == second.st_size
        and getattr(first, "st_ctime_ns", None) == getattr(second, "st_ctime_ns", None)
    )


def _assert_scan_entry_casefold_unique(
    entries: Sequence[os.DirEntry[str]], directory: Path
) -> None:
    seen: dict[str, str] = {}
    for entry in entries:
        folded = entry.name.casefold()
        prior = seen.get(folded)
        if prior is not None and prior != entry.name:
            raise ScanError(
                "directory contains Unicode-casefold-colliding entries: "
                f"{directory}: {prior!r} and {entry.name!r}"
            )
        seen[folded] = entry.name


def _entry_directory_details(entry: os.DirEntry[str]) -> tuple[bool, bool, bool]:
    """Return ``(is_directory, is_symlink, is_reparse)`` without recursion."""

    try:
        lstat_result = entry.stat(follow_symlinks=False)
        is_symlink = entry.is_symlink()
        is_reparse = _is_reparse(lstat_result)
        is_directory = stat.S_ISDIR(lstat_result.st_mode)
        if is_symlink or is_reparse:
            try:
                is_directory = is_directory or entry.is_dir(follow_symlinks=True)
            except OSError:
                # An unresolvable reparse entry is reported as skipped below if its
                # own metadata identifies it as a directory.  A broken file link is
                # treated as a direct file and never followed.
                pass
        return is_directory, is_symlink, is_reparse
    except OSError as exc:
        raise ScanError(f"cannot inspect directory entry {entry.path}: {exc}") from exc


def _file_snapshot(entry: os.DirEntry[str], root: Path) -> dict[str, Any]:
    """Hash one verified direct regular file without following a reparse entry."""

    path = Path(entry.path)
    try:
        # ``DirEntry.stat`` reports zero file IDs on some Windows/Python builds;
        # path.lstat supplies the handle-comparable identity without following.
        before = path.lstat()
        is_symlink = entry.is_symlink()
    except OSError as exc:
        raise ScanError(f"cannot stat direct file {entry.path}: {exc}") from exc
    if is_symlink or _is_reparse(before):
        raise ScanError(f"direct file is a symlink or reparse point: {entry.path}")
    if not stat.S_ISREG(before.st_mode):
        raise ScanError(f"direct entry is not a regular file: {entry.path}")

    try:
        resolved_before = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanError(f"cannot resolve direct file {entry.path}: {exc}") from exc
    if not _is_within_root(resolved_before, root):
        raise ScanError(f"direct file resolves outside the scan root: {entry.path}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ScanError(f"cannot safely open direct file {entry.path}: {exc}") from exc

    try:
        opened = os.fstat(descriptor)
        try:
            after_open = path.lstat()
            resolved_after = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ScanError(
                f"direct file changed while opening {entry.path}: {exc}"
            ) from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after_open.st_mode)
            or _is_reparse(opened)
            or _is_reparse(after_open)
            or not _same_file_identity(before, opened)
            or not _same_file_identity(opened, after_open)
            or not _is_within_root(resolved_after, root)
        ):
            raise ScanError(
                f"direct file changed identity or escaped root: {entry.path}"
            )

        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _FILE_HASH_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        finished = os.fstat(descriptor)
        try:
            final_path_state = path.lstat()
            resolved_final = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ScanError(
                f"direct file changed after hashing {entry.path}: {exc}"
            ) from exc
        if (
            not _same_file_identity(opened, finished)
            or not _same_file_identity(finished, final_path_state)
            or _is_reparse(final_path_state)
            or path.is_symlink()
            or not _is_within_root(resolved_final, root)
            or opened.st_size != finished.st_size
            or _mtime_ns(opened) != _mtime_ns(finished)
        ):
            raise ScanError(f"direct file changed while hashing: {entry.path}")
    finally:
        os.close(descriptor)

    return {
        "name": entry.name,
        "path": entry.name,
        "file_type": "regular",
        "size": int(opened.st_size),
        "mtime_ns": _mtime_ns(opened),
        "sha256": digest.hexdigest(),
    }


def _fingerprint(value: Any) -> str:
    payload = contract.stable_json_dumps(value).encode("utf-8")
    return contract.sha256_bytes(payload)


def _directory_sort_key(record: Mapping[str, Any]) -> tuple[int, str, str]:
    path = str(record["path"])
    return (-int(record.get("depth", 0)), path.casefold(), path)


def _add_unknown_scope(
    scopes: list[dict[str, str]],
    *,
    path: str,
    reason: str,
    scope: str,
) -> None:
    record = {"path": path, "reason": reason, "scope": scope}
    if record not in scopes:
        scopes.append(record)


def _skipped_directory(
    entry: os.DirEntry[str],
    *,
    relative_path: str,
    root: Path,
    is_symlink: bool,
    is_reparse: bool,
) -> tuple[dict[str, str], None]:
    try:
        resolved = Path(entry.path).resolve(strict=True)
        outside = not _is_within_root(resolved, root)
        if outside:
            target = "<outside-root>"
            reason = "outside-root"
        else:
            relative_target = resolved.relative_to(root).as_posix()
            target = relative_target if relative_target != "." else "<project-root>"
            reason = "reparse-directory"
    except (OSError, RuntimeError):
        target = "<unresolvable>"
        reason = "unresolvable-reparse-directory"

    kind = "symlink" if is_symlink else "reparse" if is_reparse else "directory"
    return (
        {
            "path": relative_path,
            "kind": kind,
            "reason": reason,
            "target": target,
        },
        None,
    )


def _validated_directory_state(path: Path, root: Path) -> os.stat_result:
    try:
        state = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanError(f"cannot validate directory {path}: {exc}") from exc
    if path.is_symlink() or _is_reparse(state) or not stat.S_ISDIR(state.st_mode):
        raise ScanError(
            f"directory became a link, reparse point, or non-directory: {path}"
        )
    if not _is_within_root(resolved, root):
        raise ScanError(f"directory escaped the scan root: {path}")
    return state


def _scan_tree_once(
    root: str | os.PathLike[str],
    *,
    executor: concurrent.futures.ThreadPoolExecutor | None = None,
) -> dict[str, Any]:
    """Return one complete safe snapshot plus skipped/unknown subtree reports."""

    resolved_root = _resolved_root(root)
    directories: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    unknown_scopes: list[dict[str, str]] = []
    visited = {os.path.normcase(str(resolved_root))}

    def inspect_child(
        entry: os.DirEntry[str],
        parent_relative: str,
    ) -> tuple[str, Path | None, dict[str, str] | None]:
        relative = _join_relative(parent_relative, entry.name)
        is_directory, is_symlink, is_reparse = _entry_directory_details(entry)
        if not is_directory:
            return relative, None, None

        if is_symlink or is_reparse:
            skipped_record, _ = _skipped_directory(
                entry,
                relative_path=relative,
                root=resolved_root,
                is_symlink=is_symlink,
                is_reparse=is_reparse,
            )
            skipped.append(skipped_record)
            _add_unknown_scope(
                unknown_scopes,
                path=relative,
                reason=skipped_record["reason"],
                scope="subtree",
            )
            return relative, None, skipped_record

        try:
            resolved = Path(entry.path).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ScanError(f"cannot resolve directory {entry.path}: {exc}") from exc
        if not _is_within_root(resolved, resolved_root):
            skipped_record = {
                "path": relative,
                "kind": "directory",
                "reason": "outside-root",
                "target": "<outside-root>",
            }
            skipped.append(skipped_record)
            _add_unknown_scope(
                unknown_scopes,
                path=relative,
                reason="outside-root",
                scope="subtree",
            )
            return relative, None, skipped_record

        identity = os.path.normcase(str(resolved))
        if identity in visited:
            relative_target = resolved.relative_to(resolved_root).as_posix()
            skipped_record = {
                "path": relative,
                "kind": "directory",
                "reason": "directory-cycle-or-alias",
                "target": (
                    relative_target if relative_target != "." else "<project-root>"
                ),
            }
            skipped.append(skipped_record)
            _add_unknown_scope(
                unknown_scopes,
                path=relative,
                reason="directory-cycle-or-alias",
                scope="subtree",
            )
            return relative, None, skipped_record
        visited.add(identity)
        return relative, resolved, None

    def list_entries(path: Path) -> list[os.DirEntry[str]]:
        before = _validated_directory_state(path, resolved_root)
        try:
            with os.scandir(path) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise ScanError(f"cannot enumerate directory {path}: {exc}") from exc
        after = _validated_directory_state(path, resolved_root)
        if not _same_file_identity(before, after):
            raise ScanError(f"directory changed identity while enumerating: {path}")
        _assert_scan_entry_casefold_unique(entries, path)
        return sorted(entries, key=lambda item: (item.name.casefold(), item.name))

    def visit(path: Path, relative: str, depth: int) -> None:
        entries = list_entries(path)
        files: list[dict[str, Any]] = []
        child_names: list[str] = []
        child_paths: list[tuple[Path, str]] = []
        local_skipped: list[dict[str, str]] = []

        candidate_files: list[os.DirEntry[str]] = []
        for entry in entries:
            is_directory, _, _ = _entry_directory_details(entry)
            if is_directory:
                if should_prune(entry.name):
                    continue
                child_names.append(entry.name)
                child_relative, child_path, skipped_record = inspect_child(
                    entry, relative
                )
                if skipped_record is not None:
                    local_skipped.append(
                        {
                            "name": entry.name,
                            "kind": skipped_record["kind"],
                            "reason": skipped_record["reason"],
                            "target": skipped_record["target"],
                        }
                    )
                elif child_path is not None:
                    child_paths.append((child_path, child_relative))
                continue

            if entry.name.startswith("."):
                continue
            if depth == 0 and entry.name.casefold() == "claude.md":
                continue
            candidate_files.append(entry)

        if executor is not None and len(candidate_files) > 1:
            files = list(
                executor.map(
                    lambda item: _file_snapshot(item, resolved_root),
                    candidate_files,
                )
            )
        else:
            files = [_file_snapshot(entry, resolved_root) for entry in candidate_files]

        child_names.sort(key=lambda value: (value.casefold(), value))
        files.sort(key=lambda value: (value["name"].casefold(), value["name"]))
        local_skipped.sort(key=lambda value: (value["name"].casefold(), value["name"]))
        is_boundary = depth > 0 and any(
            file_info["name"].casefold() == "claude.md" for file_info in files
        )
        snapshot = {
            "files": files,
            "child_directories": child_names,
            "is_boundary": is_boundary,
            "skipped_directories": local_skipped,
        }
        visible_names = {file_info["name"].casefold() for file_info in files}
        file_count = len(files)
        record = {
            "path": relative,
            "depth": depth,
            "is_boundary": is_boundary,
            "file_count": file_count,
            "py_file_count": sum(
                1 for file_info in files if file_info["name"].casefold().endswith(".py")
            ),
            "has_init_py": "__init__.py" in visible_names,
            "has_readme": "readme.md" in visible_names,
            "is_empty": file_count == 0
            or (file_count == 1 and "__init__.py" in visible_names),
            "snapshot": snapshot,
            "fingerprint": _fingerprint(snapshot),
        }
        directories.append(record)

        if is_boundary:
            _add_unknown_scope(
                unknown_scopes,
                path=relative,
                reason="boundary",
                scope="descendants",
            )
            return
        if depth >= MAX_DEPTH:
            if child_names:
                _add_unknown_scope(
                    unknown_scopes,
                    path=relative,
                    reason="max-depth",
                    scope="descendants",
                )
            return
        for child_path, child_relative in child_paths:
            visit(child_path, child_relative, depth + 1)

    # The project root itself has depth 0 and generates the top-level index.md
    # document. Root's own CLAUDE.md is the central instruction file and does
    # not form a boundary; nested visits proceed to depth 1.
    visit(resolved_root, "", 0)

    directories.sort(key=_directory_sort_key)
    skipped.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    unknown_scopes.sort(
        key=lambda value: (value["path"].casefold(), value["path"], value["reason"])
    )
    return {
        "root": resolved_root.as_posix(),
        "directories": directories,
        "skipped_directories": skipped,
        "unknown_scopes": unknown_scopes,
    }


def _scan_fault(point: str, root: Path) -> None:
    """测试故障注入点；生产运行不执行任何操作。"""

    return None


def _stable_scan_payload(scan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "directories": copy.deepcopy(scan["directories"]),
        "skipped_directories": copy.deepcopy(scan["skipped_directories"]),
        "unknown_scopes": copy.deepcopy(scan["unknown_scopes"]),
    }


def _scan_tree(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Require two consecutive equal full scans or fail after bounded retries."""

    resolved_root = _resolved_root(root)
    max_workers = min(32, (os.cpu_count() or 4) * 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        previous: dict[str, Any] | None = None
        for pass_index in range(_STABLE_SCAN_MAX_PASSES):
            current = _scan_tree_once(resolved_root, executor=executor)
            if previous is not None and _stable_scan_payload(
                previous
            ) == _stable_scan_payload(current):
                return current
            previous = current
            _scan_fault(f"after-pass-{pass_index + 1}", resolved_root)
        raise ScanError(
            f"source tree did not reach a stable two-pass snapshot after "
            f"{_STABLE_SCAN_MAX_PASSES} passes"
        )


def scan_directories(root: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Compatibility API returning the complete deepest-first directory list."""

    return _scan_tree(root)["directories"]


def compute_mtime(path: str | os.PathLike[str]) -> float:
    """Legacy non-recursive maximum file mtime helper."""

    directory = Path(path)
    try:
        mtimes = [
            entry.stat(follow_symlinks=False).st_mtime
            for entry in os.scandir(directory)
            if not entry.is_dir(follow_symlinks=False)
        ]
    except OSError:
        return 0.0
    return max(mtimes, default=0.0)


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ScanStateError(f"{field} must be a JSON object")
    return value


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ScanStateError(f"{field} must be a JSON array")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    required: set[str],
    field: str,
    *,
    optional: set[str] | None = None,
) -> None:
    actual = set(value)
    allowed = required | (optional or set())
    missing = sorted(required - actual)
    extra = sorted(actual - allowed)
    if missing or extra:
        raise ScanStateError(
            f"{field} has invalid fields (missing={missing}, extra={extra})"
        )


def _require_non_negative_int(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ScanStateError(f"{field} must be a non-negative integer")
    return value


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ScanStateError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def _validate_entry_name(name: Any, field: str) -> str:
    if (
        not isinstance(name, str)
        or not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\0" in name
    ):
        raise ScanStateError(f"{field} is not a portable direct-entry name")
    try:
        contract.validate_manifest_relative_path(f"_scan_entries/{name}/__entry__.md")
    except contract.PathValidationError as exc:
        raise ScanStateError(f"{field} is not portable: {exc}") from exc
    return name


def _ensure_casefold_unique(values: Sequence[str], field: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        folded = value.casefold()
        prior = seen.get(folded)
        if prior is not None:
            raise ScanStateError(
                f"{field} contains Unicode-casefold collision: {prior!r} and {value!r}"
            )
        seen[folded] = value


def _validate_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ScanStateError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ScanStateError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ScanStateError(f"{field} must include a timezone")
    return value


def _validate_portable_state_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ScanStateError("state_path must be a portable relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ScanStateError("state_path must be a portable relative path")
    try:
        contract.validate_manifest_relative_path(f"_scan_state_paths/{value}.md")
    except contract.PathValidationError as exc:
        raise ScanStateError(f"state_path is not portable: {exc}") from exc
    return value


def _validate_directory_snapshot(record: Mapping[str, Any], field: str) -> str:
    required_record_fields = {
        "path",
        "depth",
        "is_boundary",
        "file_count",
        "py_file_count",
        "has_init_py",
        "has_readme",
        "is_empty",
        "snapshot",
        "fingerprint",
    }
    _require_exact_keys(record, required_record_fields, field)
    path = record.get("path")
    if not isinstance(path, str):
        raise ScanStateError(f"{field}.path must be a string")
    _validate_relative_directory_path(path)

    depth = _require_non_negative_int(record.get("depth"), f"{field}.depth")
    expected_depth = len(PurePosixPath(path).parts)
    if depth != expected_depth or depth < 0 or depth > MAX_DEPTH:
        raise ScanStateError(f"{field}.depth does not match its relative path")

    snapshot = _require_mapping(record.get("snapshot"), f"{field}.snapshot")
    _require_exact_keys(
        snapshot,
        {"files", "child_directories", "is_boundary", "skipped_directories"},
        f"{field}.snapshot",
    )
    files = _require_list(snapshot.get("files"), f"{field}.snapshot.files")
    file_names: list[str] = []
    for index, file_info_value in enumerate(files):
        file_field = f"{field}.snapshot.files[{index}]"
        file_info = _require_mapping(file_info_value, file_field)
        _require_exact_keys(
            file_info,
            {"name", "path", "file_type", "size", "mtime_ns", "sha256"},
            file_field,
        )
        name = _validate_entry_name(file_info.get("name"), f"{file_field}.name")
        if file_info.get("path") != name:
            raise ScanStateError(f"{file_field}.path must equal its direct file name")
        if file_info.get("file_type") != "regular":
            raise ScanStateError(f"{file_field}.file_type must be 'regular'")
        _require_non_negative_int(file_info.get("size"), f"{file_field}.size")
        _require_non_negative_int(file_info.get("mtime_ns"), f"{file_field}.mtime_ns")
        _require_sha256(file_info.get("sha256"), f"{file_field}.sha256")
        file_names.append(name)
    if files != sorted(
        files,
        key=lambda value: (str(value["name"]).casefold(), str(value["name"])),
    ):
        raise ScanStateError(f"{field}.snapshot.files must use canonical order")

    child_values = _require_list(
        snapshot.get("child_directories"),
        f"{field}.snapshot.child_directories",
    )
    child_names = [
        _validate_entry_name(value, f"{field}.snapshot.child_directories[{index}]")
        for index, value in enumerate(child_values)
    ]
    if child_names != sorted(child_names, key=lambda value: (value.casefold(), value)):
        raise ScanStateError(
            f"{field}.snapshot.child_directories must use canonical order"
        )
    _ensure_casefold_unique(file_names + child_names, f"{field}.snapshot entries")

    skipped_values = _require_list(
        snapshot.get("skipped_directories"),
        f"{field}.snapshot.skipped_directories",
    )
    skipped_names: list[str] = []
    for index, skipped_value in enumerate(skipped_values):
        skipped_field = f"{field}.snapshot.skipped_directories[{index}]"
        skipped = _require_mapping(skipped_value, skipped_field)
        _require_exact_keys(
            skipped, {"name", "kind", "reason", "target"}, skipped_field
        )
        name = _validate_entry_name(skipped.get("name"), f"{skipped_field}.name")
        if name not in child_names:
            raise ScanStateError(f"{skipped_field}.name is not a child directory")
        if skipped.get("kind") not in ("symlink", "reparse", "directory"):
            raise ScanStateError(f"{skipped_field}.kind is invalid")
        if skipped.get("reason") not in (
            "outside-root",
            "reparse-directory",
            "unresolvable-reparse-directory",
            "directory-cycle-or-alias",
        ):
            raise ScanStateError(f"{skipped_field}.reason is invalid")
        target = skipped.get("target")
        if target not in ("<outside-root>", "<unresolvable>", "<project-root>"):
            if not isinstance(target, str):
                raise ScanStateError(f"{skipped_field}.target is invalid")
            _validate_relative_directory_path(target)
        skipped_names.append(name)
    _ensure_casefold_unique(skipped_names, f"{field}.snapshot.skipped_directories")
    if skipped_values != sorted(
        skipped_values,
        key=lambda value: (str(value["name"]).casefold(), str(value["name"])),
    ):
        raise ScanStateError(
            f"{field}.snapshot.skipped_directories must use canonical order"
        )

    is_boundary = snapshot.get("is_boundary")
    if type(is_boundary) is not bool:
        raise ScanStateError(f"{field}.snapshot.is_boundary must be boolean")
    expected_boundary = depth > 0 and any(
        name.casefold() == "claude.md" for name in file_names
    )
    if (
        is_boundary != expected_boundary
        or record.get("is_boundary") != expected_boundary
    ):
        raise ScanStateError(f"{field}.is_boundary is inconsistent with direct files")

    visible_names = {name.casefold() for name in file_names}
    _require_non_negative_int(record.get("file_count"), f"{field}.file_count")
    _require_non_negative_int(record.get("py_file_count"), f"{field}.py_file_count")
    for boolean_field in ("is_boundary", "has_init_py", "has_readme", "is_empty"):
        if type(record.get(boolean_field)) is not bool:
            raise ScanStateError(f"{field}.{boolean_field} must be boolean")
    expected_values = {
        "file_count": len(files),
        "py_file_count": sum(name.casefold().endswith(".py") for name in file_names),
        "has_init_py": "__init__.py" in visible_names,
        "has_readme": "readme.md" in visible_names,
        "is_empty": len(files) == 0
        or (len(files) == 1 and "__init__.py" in visible_names),
    }
    for derived_field, expected_value in expected_values.items():
        if record.get(derived_field) != expected_value:
            raise ScanStateError(
                f"{field}.{derived_field} is inconsistent with snapshot"
            )

    fingerprint = _require_sha256(record.get("fingerprint"), f"{field}.fingerprint")
    if fingerprint != _fingerprint(dict(snapshot)):
        raise ScanStateError(f"{field}.fingerprint does not match its snapshot")
    return path


def _validate_skipped_directory(record: Mapping[str, Any], field: str) -> str:
    _require_exact_keys(record, {"path", "kind", "reason", "target"}, field)
    path = record.get("path")
    if not isinstance(path, str):
        raise ScanStateError(f"{field}.path must be a string")
    _validate_relative_directory_path(path)
    if record.get("kind") not in ("symlink", "reparse", "directory"):
        raise ScanStateError(f"{field}.kind is invalid")
    if record.get("reason") not in (
        "outside-root",
        "reparse-directory",
        "unresolvable-reparse-directory",
        "directory-cycle-or-alias",
    ):
        raise ScanStateError(f"{field}.reason is invalid")
    target = record.get("target")
    if target not in ("<outside-root>", "<unresolvable>", "<project-root>"):
        if not isinstance(target, str):
            raise ScanStateError(f"{field}.target is invalid")
        _validate_relative_directory_path(target)
    return path


def _validate_unknown_scope(record: Mapping[str, Any], field: str) -> str:
    _require_exact_keys(record, {"path", "reason", "scope"}, field)
    path = record.get("path")
    if not isinstance(path, str):
        raise ScanStateError(f"{field}.path must be a string")
    _validate_relative_directory_path(path)
    if record.get("reason") not in (
        "boundary",
        "max-depth",
        "outside-root",
        "reparse-directory",
        "unresolvable-reparse-directory",
        "directory-cycle-or-alias",
    ):
        raise ScanStateError(f"{field}.reason is invalid")
    if record.get("scope") not in ("descendants", "subtree"):
        raise ScanStateError(f"{field}.scope is invalid")
    return path


def _ensure_unique_directory_paths(paths: Sequence[str], field: str) -> None:
    try:
        contract.ensure_unique_manifest_paths(
            [f"{path}/__scan_directory__.md" if path else "index.md" for path in paths]
        )
    except contract.PathValidationError as exc:
        raise ScanStateError(f"{field} contains conflicting paths: {exc}") from exc


def _validate_complete_snapshot(
    snapshot_value: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot = dict(_require_mapping(snapshot_value, "snapshot"))
    _require_exact_keys(
        snapshot,
        {"directories", "skipped_directories", "unknown_scopes"},
        "snapshot",
    )
    record_values = _require_list(snapshot.get("directories"), "snapshot.directories")
    records: list[dict[str, Any]] = []
    paths: list[str] = []
    for index, record_value in enumerate(record_values):
        field = f"snapshot.directories[{index}]"
        record = dict(_require_mapping(record_value, field))
        paths.append(_validate_directory_snapshot(record, field))
        records.append(record)
    _ensure_unique_directory_paths(paths, "snapshot.directories")
    if records != sorted(records, key=_directory_sort_key):
        raise ScanStateError(
            "snapshot.directories must use canonical deepest-first order"
        )

    skipped_values = _require_list(
        snapshot.get("skipped_directories"), "snapshot.skipped_directories"
    )
    skipped_paths = [
        _validate_skipped_directory(
            _require_mapping(value, f"snapshot.skipped_directories[{index}]"),
            f"snapshot.skipped_directories[{index}]",
        )
        for index, value in enumerate(skipped_values)
    ]
    _ensure_unique_directory_paths(skipped_paths, "snapshot.skipped_directories")
    if skipped_values != sorted(
        skipped_values,
        key=lambda value: (str(value["path"]).casefold(), str(value["path"])),
    ):
        raise ScanStateError("snapshot.skipped_directories must use canonical order")

    scope_values = _require_list(
        snapshot.get("unknown_scopes"), "snapshot.unknown_scopes"
    )
    scope_paths = [
        _validate_unknown_scope(
            _require_mapping(value, f"snapshot.unknown_scopes[{index}]"),
            f"snapshot.unknown_scopes[{index}]",
        )
        for index, value in enumerate(scope_values)
    ]
    _ensure_unique_directory_paths(scope_paths, "snapshot.unknown_scopes")
    if scope_values != sorted(
        scope_values,
        key=lambda value: (
            str(value["path"]).casefold(),
            str(value["path"]),
            str(value["reason"]),
        ),
    ):
        raise ScanStateError("snapshot.unknown_scopes must use canonical order")
    return snapshot, records


def _validate_changes(value: Any, current_paths: Sequence[str]) -> dict[str, list[str]]:
    changes_value = _require_mapping(value, "changes")
    names = ("new", "modified", "deleted", "unchanged", "unknown")
    _require_exact_keys(changes_value, set(names), "changes")
    changes: dict[str, list[str]] = {}
    all_paths: list[str] = []
    for name in names:
        paths = _require_list(changes_value.get(name), f"changes.{name}")
        validated: list[str] = []
        for index, path in enumerate(paths):
            if not isinstance(path, str):
                raise ScanStateError(f"changes.{name}[{index}] must be a string")
            validated.append(_validate_relative_directory_path(path))
        if validated != sorted(validated, key=lambda item: (item.casefold(), item)):
            raise ScanStateError(f"changes.{name} must use canonical order")
        changes[name] = validated
        all_paths.extend(validated)
    _ensure_unique_directory_paths(all_paths, "changes")

    current_by_fold = {path.casefold(): path for path in current_paths}
    classified_current = changes["new"] + changes["modified"] + changes["unchanged"]
    if {path.casefold() for path in classified_current} != set(current_by_fold):
        raise ScanStateError(
            "changes.new/modified/unchanged must partition snapshot.directories"
        )
    for path in classified_current:
        if current_by_fold.get(path.casefold()) != path:
            raise ScanStateError(
                "changes must preserve canonical snapshot path spelling"
            )
    for path in changes["deleted"] + changes["unknown"]:
        if path.casefold() in current_by_fold:
            raise ScanStateError(
                "deleted/unknown changes must not exist in the snapshot"
            )
    return changes


def _candidate_worklist(
    data: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    changes: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    change_by_fold = {
        path.casefold(): name
        for name in ("new", "modified", "unchanged")
        for path in changes[name]
    }
    if data.get("mode") == "incremental":
        selected = {path.casefold() for path in changes["new"] + changes["modified"]}
    else:
        selected = {str(record["path"]).casefold() for record in records}
    worklist: list[dict[str, Any]] = []
    for record in records:
        path = str(record["path"])
        if path.casefold() not in selected:
            continue
        annotated = copy.deepcopy(dict(record))
        annotated["change"] = change_by_fold[path.casefold()]
        worklist.append(annotated)
    return worklist


def _candidate_generation_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(dict(data))
    payload.pop("candidate_generation_id", None)
    payload.pop("committed_at", None)
    payload.pop("state_path", None)
    payload.pop("manifest_generation_id", None)
    if payload.get("state_role") == "committed":
        snapshot = _require_mapping(payload.get("snapshot"), "snapshot")
        records = _require_list(snapshot.get("directories"), "snapshot.directories")
        changes = _require_mapping(payload.get("changes"), "changes")
        payload["state_role"] = "candidate"
        payload["directories"] = _candidate_worklist(payload, records, changes)
        payload["total"] = len(payload["directories"])
    return payload


def _candidate_generation_id(data: Mapping[str, Any]) -> str:
    return _fingerprint(_candidate_generation_payload(data))


def validate_scan_state_v2(state: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly validate every redundant v2 field and its candidate binding."""

    data = copy.deepcopy(dict(_require_mapping(state, "scan state")))
    role = data.get("state_role")
    base_fields = {
        "schema_version",
        "kind",
        "state_role",
        "producer",
        "root",
        "project_identity",
        "scan_time",
        "mode",
        "baseline",
        "directories",
        "deleted_directories",
        "unknown_directories",
        "changes",
        "change_counts",
        "total",
        "full_total",
        "skipped_unchanged",
        "max_depth",
        "boundary_count",
        "skipped_directory_count",
        "snapshot",
        "snapshot_fingerprint",
        "candidate_generation_id",
    }
    if role == "candidate":
        _require_exact_keys(
            data,
            base_fields,
            "candidate scan state",
            optional={"baseline_precondition"},
        )
    elif role == "committed":
        _require_exact_keys(
            data,
            base_fields | {"committed_at"},
            "committed scan state",
            optional={
                "state_path",
                "baseline_precondition",
                "manifest_generation_id",
            },
        )
    else:
        raise ScanStateError("state_role must be 'candidate' or 'committed'")

    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != SCAN_STATE_SCHEMA_VERSION:
        raise ScanStateError(
            f"expected schema_version={SCAN_STATE_SCHEMA_VERSION}, got {schema_version!r}"
        )
    if data.get("kind") != SCAN_STATE_KIND:
        raise ScanStateError(f"scan state kind must be {SCAN_STATE_KIND!r}")
    if data.get("producer") != SCAN_STATE_PRODUCER:
        raise ScanStateError("producer does not match this scan-state contract")
    if data.get("root") != _PORTABLE_ROOT:
        raise ScanStateError("v2 root must be the portable project-relative value '.'")
    _require_sha256(data.get("project_identity"), "project_identity")
    _validate_timestamp(data.get("scan_time"), "scan_time")
    if role == "committed":
        _validate_timestamp(data.get("committed_at"), "committed_at")
        if "state_path" in data:
            _validate_portable_state_path(data["state_path"])
        if "manifest_generation_id" in data:
            _require_sha256(
                data.get("manifest_generation_id"),
                "manifest_generation_id",
            )

    mode = data.get("mode")
    if mode not in ("incremental", "full"):
        raise ScanStateError("mode must be 'incremental' or 'full'")
    baseline = _require_mapping(data.get("baseline"), "baseline")
    _require_exact_keys(
        baseline,
        {
            "available",
            "schema_version",
            "snapshot_fingerprint",
            "candidate_generation_id",
            "force_full",
        },
        "baseline",
    )
    if type(baseline.get("available")) is not bool:
        raise ScanStateError("baseline.available must be boolean")
    if type(baseline.get("force_full")) is not bool:
        raise ScanStateError("baseline.force_full must be boolean")
    if baseline["force_full"]:
        if mode != "full" or baseline["available"]:
            raise ScanStateError(
                "force_full must use full mode and ignore the baseline"
            )
    if baseline["available"]:
        baseline_schema = baseline.get("schema_version")
        if type(baseline_schema) is not int or baseline_schema not in (1, 2):
            raise ScanStateError("baseline.schema_version must be 1 or 2")
        if baseline_schema == 2:
            _require_sha256(
                baseline.get("snapshot_fingerprint"),
                "baseline.snapshot_fingerprint",
            )
            _require_sha256(
                baseline.get("candidate_generation_id"),
                "baseline.candidate_generation_id",
            )
        elif (
            baseline.get("snapshot_fingerprint") is not None
            or baseline.get("candidate_generation_id") is not None
        ):
            raise ScanStateError("legacy baseline fingerprints must be null")
    elif any(
        baseline.get(name) is not None
        for name in (
            "schema_version",
            "snapshot_fingerprint",
            "candidate_generation_id",
        )
    ):
        raise ScanStateError("unavailable baseline metadata must be null")

    if "baseline_precondition" in data:
        precondition = _require_mapping(
            data.get("baseline_precondition"),
            "baseline_precondition",
        )
        _require_exact_keys(
            precondition,
            {"exists", "sha256", "generation_id"},
            "baseline_precondition",
        )
        if type(precondition.get("exists")) is not bool:
            raise ScanStateError("baseline_precondition.exists must be boolean")
        if precondition["exists"]:
            _require_sha256(
                precondition.get("sha256"),
                "baseline_precondition.sha256",
            )
            if precondition.get("generation_id") is not None:
                _require_sha256(
                    precondition.get("generation_id"),
                    "baseline_precondition.generation_id",
                )
        elif (
            precondition.get("sha256") is not None
            or precondition.get("generation_id") is not None
        ):
            raise ScanStateError("absent baseline_precondition cannot carry hashes")
        if not baseline["force_full"]:
            if precondition["exists"] != baseline["available"]:
                raise ScanStateError(
                    "baseline_precondition existence disagrees with baseline"
                )
            if (
                baseline["available"]
                and baseline["schema_version"] == SCAN_STATE_SCHEMA_VERSION
                and precondition.get("generation_id")
                != baseline["candidate_generation_id"]
            ):
                raise ScanStateError(
                    "baseline_precondition generation disagrees with baseline"
                )

    snapshot, records = _validate_complete_snapshot(data.get("snapshot"))
    snapshot_fingerprint = _require_sha256(
        data.get("snapshot_fingerprint"), "snapshot_fingerprint"
    )
    if snapshot_fingerprint != _fingerprint(snapshot):
        raise ScanStateError("snapshot_fingerprint does not match snapshot")
    current_paths = [str(record["path"]) for record in records]
    changes = _validate_changes(data.get("changes"), current_paths)

    if baseline["force_full"]:
        expected_new = sorted(current_paths, key=lambda item: (item.casefold(), item))
        if changes["new"] != expected_new or any(
            changes[name] for name in ("modified", "deleted", "unchanged", "unknown")
        ):
            raise ScanStateError(
                "force_full must classify every current directory as new"
            )

    counts_value = _require_mapping(data.get("change_counts"), "change_counts")
    _require_exact_keys(counts_value, set(changes), "change_counts")
    expected_counts = {name: len(paths) for name, paths in changes.items()}
    for name in expected_counts:
        _require_non_negative_int(counts_value.get(name), f"change_counts.{name}")
    if dict(counts_value) != expected_counts:
        raise ScanStateError("change_counts does not match changes")

    expected_worklist = _candidate_worklist(data, records, changes)
    directory_values = _require_list(data.get("directories"), "directories")
    expected_directories: Sequence[Mapping[str, Any]] = (
        expected_worklist if role == "candidate" else records
    )
    if directory_values != list(expected_directories):
        raise ScanStateError("directories does not match mode, changes, and snapshot")

    expected_deleted = [
        {"path": path, "change": "deleted"} for path in changes["deleted"]
    ]
    expected_unknown = [
        {"path": path, "change": "unknown"} for path in changes["unknown"]
    ]
    if (
        _require_list(data.get("deleted_directories"), "deleted_directories")
        != expected_deleted
    ):
        raise ScanStateError("deleted_directories does not match changes.deleted")
    if (
        _require_list(data.get("unknown_directories"), "unknown_directories")
        != expected_unknown
    ):
        raise ScanStateError("unknown_directories does not match changes.unknown")

    full_total = _require_non_negative_int(data.get("full_total"), "full_total")
    if full_total != len(records):
        raise ScanStateError("full_total does not match snapshot.directories")
    total = _require_non_negative_int(data.get("total"), "total")
    expected_total = len(expected_worklist) if role == "candidate" else full_total
    if total != expected_total:
        raise ScanStateError("total does not match directories")
    skipped_unchanged = _require_non_negative_int(
        data.get("skipped_unchanged"), "skipped_unchanged"
    )
    expected_skipped = len(changes["unchanged"]) if mode == "incremental" else 0
    if skipped_unchanged != expected_skipped:
        raise ScanStateError("skipped_unchanged is inconsistent with mode and changes")

    max_depth = _require_non_negative_int(data.get("max_depth"), "max_depth")
    if max_depth != max((int(record["depth"]) for record in records), default=0):
        raise ScanStateError("max_depth does not match snapshot.directories")
    boundary_count = _require_non_negative_int(
        data.get("boundary_count"), "boundary_count"
    )
    if boundary_count != sum(bool(record["is_boundary"]) for record in records):
        raise ScanStateError("boundary_count does not match snapshot.directories")
    skipped_count = _require_non_negative_int(
        data.get("skipped_directory_count"), "skipped_directory_count"
    )
    if skipped_count != len(snapshot["skipped_directories"]):
        raise ScanStateError("skipped_directory_count does not match snapshot")

    generation = _require_sha256(
        data.get("candidate_generation_id"), "candidate_generation_id"
    )
    if not hmac.compare_digest(generation, _candidate_generation_id(data)):
        raise ScanStateError(
            "candidate_generation_id does not cover the complete candidate"
        )
    return data


def _legacy_state_from_paths(
    paths: Sequence[str], project_identity: str | None = None
) -> dict[str, Any]:
    validated = [_validate_relative_directory_path(path) for path in paths]
    _ensure_unique_directory_paths(validated, "legacy directories")
    normalized: dict[str, Any] = {
        "schema_version": 1,
        "directories": [
            {"path": path}
            for path in sorted(validated, key=lambda item: (item.casefold(), item))
        ],
    }
    if project_identity is not None:
        normalized["legacy_project_identity"] = _require_sha256(
            project_identity, "legacy_project_identity"
        )
    return normalized


def _validate_legacy_state(state: Mapping[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(dict(state))
    directories = _require_list(data.get("directories"), "legacy directories")
    paths: list[str] = []
    for index, value in enumerate(directories):
        record = _require_mapping(value, f"legacy directories[{index}]")
        path = record.get("path")
        if not isinstance(path, str):
            raise ScanStateError(f"legacy directories[{index}].path must be a string")
        paths.append(path)

    identity: str | None = None
    if "legacy_project_identity" in data:
        identity = _require_sha256(
            data.get("legacy_project_identity"), "legacy_project_identity"
        )
    legacy_root = data.get("root")
    if legacy_root is not None:
        if not isinstance(legacy_root, str) or not Path(legacy_root).is_absolute():
            raise ScanStateError("legacy root must be an absolute path")
        root_identity = _project_identity_from_path(Path(legacy_root))
        if identity is not None and not hmac.compare_digest(identity, root_identity):
            raise ScanStateError("legacy root and project identity disagree")
        identity = root_identity
    return _legacy_state_from_paths(paths, identity)


def _migrate_legacy_v2_state(
    state: Mapping[str, Any], *, require_committed: bool
) -> dict[str, Any]:
    """Reduce the former absolute-root v2 state to a conservative v1 baseline."""

    data = copy.deepcopy(dict(state))
    if data.get("schema_version") != SCAN_STATE_SCHEMA_VERSION:
        raise ScanStateError("legacy v2 state has an invalid schema version")
    if require_committed and data.get("state_role") != "committed":
        raise ScanStateError("only committed state can be used as a baseline")
    root = data.get("root")
    if not isinstance(root, str) or not Path(root).is_absolute():
        raise ScanStateError("legacy v2 root must be an absolute path")

    snapshot = _require_mapping(data.get("snapshot"), "legacy v2 snapshot")
    records = _require_list(
        snapshot.get("directories"), "legacy v2 snapshot.directories"
    )
    paths: list[str] = []
    for index, value in enumerate(records):
        field = f"legacy v2 snapshot.directories[{index}]"
        record = _require_mapping(value, field)
        path = record.get("path")
        if not isinstance(path, str):
            raise ScanStateError(f"{field}.path must be a string")
        paths.append(_validate_relative_directory_path(path))
        record_snapshot = record.get("snapshot")
        record_fingerprint = record.get("fingerprint")
        if record_snapshot is not None or record_fingerprint is not None:
            if not isinstance(record_snapshot, Mapping):
                raise ScanStateError(f"{field}.snapshot must be an object")
            if record_fingerprint != _fingerprint(dict(record_snapshot)):
                raise ScanStateError(f"{field}.fingerprint does not match snapshot")
    if data.get("snapshot_fingerprint") != _fingerprint(dict(snapshot)):
        raise ScanStateError("legacy v2 snapshot_fingerprint does not match snapshot")
    return _legacy_state_from_paths(paths, _project_identity_from_path(Path(root)))


def parse_previous_state_bytes(
    raw_bytes: bytes | None,
    *,
    source: str = "<scan-state-bytes>",
) -> dict[str, Any] | None:
    """Parse one captured byte snapshot into the committed baseline it represents."""

    if raw_bytes is None:
        return None
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScanStateError(
            f"scan state is not strict UTF-8: {source}: {exc}"
        ) from exc
    value = contract.parse_json_strict(text, source=source)
    mapping = _require_mapping(value, "scan state")
    if "schema_version" not in mapping or mapping.get("schema_version") == 1:
        return _validate_legacy_state(mapping)
    if mapping.get("schema_version") != SCAN_STATE_SCHEMA_VERSION:
        raise ScanStateError("unsupported scan-state schema version")
    if (
        mapping.get("root") != _PORTABLE_ROOT
        or "project_identity" not in mapping
        or "candidate_generation_id" not in mapping
    ):
        return _migrate_legacy_v2_state(mapping, require_committed=True)
    validated = validate_scan_state_v2(mapping)
    if validated["state_role"] != "committed":
        raise ScanStateError("only committed state can be used as a baseline")
    return validated


def _read_optional_regular_bytes(path: Path, label: str) -> bytes | None:
    lexical = Path(os.path.abspath(path))
    if not os.path.lexists(lexical):
        return None
    try:
        before = lexical.lstat()
        resolved_before = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanStateError(f"cannot inspect {label} {path}: {exc}") from exc
    if (
        lexical.is_symlink()
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or os.path.normcase(os.path.normpath(str(lexical)))
        != os.path.normcase(os.path.normpath(str(resolved_before)))
    ):
        raise ScanStateError(
            f"{label} must be a direct regular non-reparse file: {path}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lexical, flags)
    except OSError as exc:
        raise ScanStateError(f"cannot safely open {label} {path}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        after_open = lexical.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(after_open.st_mode)
            or _is_reparse(opened)
            or _is_reparse(after_open)
            or lexical.is_symlink()
            or not _same_file_identity(before, opened)
            or not _same_file_identity(opened, after_open)
        ):
            raise ScanStateError(f"{label} changed identity while opening: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, _FILE_HASH_CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        finished = os.fstat(descriptor)
        final_path_state = lexical.lstat()
        resolved_final = lexical.resolve(strict=True)
        if (
            not _same_file_identity(opened, finished)
            or not _same_file_identity(finished, final_path_state)
            or _is_reparse(final_path_state)
            or lexical.is_symlink()
            or os.path.normcase(os.path.normpath(str(lexical)))
            != os.path.normcase(os.path.normpath(str(resolved_final)))
            or opened.st_size != finished.st_size
            or _mtime_ns(opened) != _mtime_ns(finished)
        ):
            raise ScanStateError(f"{label} changed while reading: {path}")
    except (OSError, RuntimeError) as exc:
        raise ScanStateError(f"cannot safely read {label} {path}: {exc}") from exc
    finally:
        os.close(descriptor)
    return b"".join(chunks)


def baseline_precondition_from_bytes(
    raw_bytes: bytes | None,
    *,
    source: str = "<scan-state-bytes>",
) -> BaselinePrecondition:
    state = parse_previous_state_bytes(raw_bytes, source=source)
    generation = None
    if state is not None and state.get("schema_version") == SCAN_STATE_SCHEMA_VERSION:
        value = state.get("candidate_generation_id")
        generation = value if isinstance(value, str) else None
    return BaselinePrecondition(
        exists=raw_bytes is not None,
        sha256=contract.sha256_bytes(raw_bytes) if raw_bytes is not None else None,
        generation_id=generation,
    )


def load_previous_state_with_precondition(
    state_path: str | os.PathLike[str],
) -> tuple[dict[str, Any] | None, BaselinePrecondition, bytes | None]:
    path = Path(state_path)
    raw_bytes = _read_optional_regular_bytes(path, "scan state")
    state = parse_previous_state_bytes(raw_bytes, source=str(path))
    generation = None
    if state is not None and state.get("schema_version") == SCAN_STATE_SCHEMA_VERSION:
        value = state.get("candidate_generation_id")
        generation = value if isinstance(value, str) else None
    precondition = BaselinePrecondition(
        exists=raw_bytes is not None,
        sha256=contract.sha256_bytes(raw_bytes) if raw_bytes is not None else None,
        generation_id=generation,
    )
    return state, precondition, raw_bytes


def load_previous_state(
    state_path: str | os.PathLike[str],
) -> dict[str, Any] | None:
    """Strictly load a committed v2 baseline or conservatively migrate legacy state."""

    state, _, _ = load_previous_state_with_precondition(state_path)
    return state


def _state_records(state: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if state is None:
        return []
    if state.get("schema_version") == SCAN_STATE_SCHEMA_VERSION:
        snapshot = _require_mapping(state.get("snapshot"), "snapshot")
        return [
            copy.deepcopy(dict(_require_mapping(record, "snapshot directory")))
            for record in _require_list(
                snapshot.get("directories"), "snapshot.directories"
            )
        ]
    # Legacy records deliberately carry paths only, so every surviving directory
    # is classified modified until a content-hash v2 baseline is committed.
    return [
        {"path": str(_require_mapping(record, "legacy directory")["path"])}
        for record in _require_list(state.get("directories"), "legacy directories")
    ]


def _scope_covers(path: str, scope: Mapping[str, str]) -> bool:
    scope_parts = tuple(part.casefold() for part in PurePosixPath(scope["path"]).parts)
    candidate_parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    if candidate_parts[: len(scope_parts)] != scope_parts:
        return False
    if scope.get("scope") == "subtree":
        return True
    return len(candidate_parts) > len(scope_parts)


def _classify(
    current: Sequence[Mapping[str, Any]],
    previous: Sequence[Mapping[str, Any]],
    unknown_scopes: Sequence[Mapping[str, str]],
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    current_map = {
        str(record["path"]).casefold(): (str(record["path"]), record)
        for record in current
    }
    previous_map = {
        str(record["path"]).casefold(): (str(record["path"]), record)
        for record in previous
    }
    changes = {
        "new": [],
        "modified": [],
        "deleted": [],
        "unchanged": [],
        "unknown": [],
    }
    records_by_path: dict[str, dict[str, Any]] = {}

    for folded, (path, record) in current_map.items():
        previous_entry = previous_map.get(folded)
        old_path, old = previous_entry if previous_entry is not None else (None, None)
        if old is None:
            change = "new"
        elif (
            old_path == path
            and isinstance(old.get("fingerprint"), str)
            and old.get("fingerprint") == record.get("fingerprint")
        ):
            change = "unchanged"
        else:
            # Legacy path-only records and spelling-only path changes both require
            # processing; a complete matching v2 fingerprint is needed to skip.
            change = "modified"
        changes[change].append(path)
        changed_record = copy.deepcopy(dict(record))
        changed_record["change"] = change
        records_by_path[path] = changed_record

    for folded, (path, _old) in previous_map.items():
        if folded in current_map:
            continue
        change = (
            "unknown"
            if any(_scope_covers(path, scope) for scope in unknown_scopes)
            else "deleted"
        )
        changes[change].append(path)
        records_by_path[path] = {"path": path, "change": change}

    for paths in changes.values():
        paths.sort(key=lambda value: (value.casefold(), value))
    return changes, records_by_path


def build_candidate_state(
    root: str | os.PathLike[str],
    previous: Mapping[str, Any] | None = None,
    *,
    incremental: bool = False,
    force_full: bool = False,
    baseline_precondition: BaselinePrecondition | None = None,
) -> dict[str, Any]:
    """Scan ``root`` and build a complete, root-bound schema-v2 candidate."""

    scan = _scan_tree(root)
    resolved_root = Path(scan["root"])
    identity = _project_identity(resolved_root)
    normalized_previous: Mapping[str, Any] | None = None
    # A forced full scan is deliberately independent of baseline validity and
    # classification.  It still emits a fresh root-bound candidate.
    if previous is not None and not force_full:
        if previous.get("schema_version") == SCAN_STATE_SCHEMA_VERSION:
            if (
                previous.get("root") == _PORTABLE_ROOT
                and "project_identity" in previous
                and "candidate_generation_id" in previous
            ):
                normalized_previous = validate_scan_state_v2(previous)
            else:
                normalized_previous = _migrate_legacy_v2_state(
                    previous, require_committed=False
                )
        else:
            normalized_previous = _validate_legacy_state(previous)
        previous_identity = normalized_previous.get(
            "project_identity",
            normalized_previous.get("legacy_project_identity"),
        )
        if previous_identity is not None and not hmac.compare_digest(
            str(previous_identity), identity
        ):
            raise ScanStateError(
                "committed scan state belongs to a different project root"
            )

    comparison_previous = None if force_full else normalized_previous
    previous_records = _state_records(comparison_previous)
    changes, classified = _classify(
        scan["directories"],
        previous_records,
        scan["unknown_scopes"],
    )
    mode = "incremental" if incremental and not force_full else "full"
    if mode == "incremental":
        selected_paths = set(changes["new"] + changes["modified"])
    else:
        selected_paths = {str(record["path"]) for record in scan["directories"]}

    directories = [
        copy.deepcopy(classified[str(record["path"])])
        for record in scan["directories"]
        if str(record["path"]) in selected_paths
    ]
    deleted_directories = [
        copy.deepcopy(classified[path]) for path in changes["deleted"]
    ]
    unknown_directories = [
        copy.deepcopy(classified[path]) for path in changes["unknown"]
    ]
    snapshot = {
        "directories": copy.deepcopy(scan["directories"]),
        "skipped_directories": copy.deepcopy(scan["skipped_directories"]),
        "unknown_scopes": copy.deepcopy(scan["unknown_scopes"]),
    }
    change_counts = {name: len(paths) for name, paths in changes.items()}
    if comparison_previous is None:
        baseline_schema = None
        baseline_snapshot_fingerprint = None
        baseline_generation = None
    else:
        baseline_schema = int(comparison_previous.get("schema_version", 1))
        baseline_snapshot_fingerprint = (
            comparison_previous.get("snapshot_fingerprint")
            if baseline_schema == SCAN_STATE_SCHEMA_VERSION
            else None
        )
        baseline_generation = (
            comparison_previous.get("candidate_generation_id")
            if baseline_schema == SCAN_STATE_SCHEMA_VERSION
            else None
        )

    bound_precondition: BaselinePrecondition | None = None
    if baseline_precondition is not None:
        bound_precondition = _validate_baseline_precondition(baseline_precondition)
        if not force_full:
            if bound_precondition.exists != (comparison_previous is not None):
                raise ScanStateError(
                    "captured baseline existence disagrees with parsed baseline"
                )
            if bound_precondition.generation_id != baseline_generation:
                raise ScanStateError(
                    "captured baseline generation disagrees with parsed baseline"
                )

    candidate = {
        "schema_version": SCAN_STATE_SCHEMA_VERSION,
        "kind": SCAN_STATE_KIND,
        "state_role": "candidate",
        "producer": copy.deepcopy(SCAN_STATE_PRODUCER),
        "root": _PORTABLE_ROOT,
        "project_identity": identity,
        "scan_time": _now_iso(),
        "mode": mode,
        "baseline": {
            "available": comparison_previous is not None,
            "schema_version": baseline_schema,
            "snapshot_fingerprint": baseline_snapshot_fingerprint,
            "candidate_generation_id": baseline_generation,
            "force_full": bool(force_full),
        },
        "directories": directories,
        "deleted_directories": deleted_directories,
        "unknown_directories": unknown_directories,
        "changes": changes,
        "change_counts": change_counts,
        "total": len(directories),
        "full_total": len(scan["directories"]),
        "skipped_unchanged": change_counts["unchanged"] if mode == "incremental" else 0,
        "max_depth": max(
            (int(record["depth"]) for record in scan["directories"]),
            default=0,
        ),
        "boundary_count": sum(
            1 for record in scan["directories"] if record["is_boundary"]
        ),
        "skipped_directory_count": len(scan["skipped_directories"]),
        "snapshot": snapshot,
        "snapshot_fingerprint": _fingerprint(snapshot),
    }
    if bound_precondition is not None:
        candidate["baseline_precondition"] = {
            "exists": bound_precondition.exists,
            "sha256": bound_precondition.sha256,
            "generation_id": bound_precondition.generation_id,
        }
    candidate["candidate_generation_id"] = _candidate_generation_id(candidate)
    return validate_scan_state_v2(candidate)


def incremental_scan(
    root: str | os.PathLike[str],
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    """Compatibility API for an incremental schema-v2 candidate scan."""

    return build_candidate_state(root, previous, incremental=True)


def _ensure_output_parent(path: Path) -> None:
    # ``Path('state.json').parent`` is ``Path('.')`` rather than the empty string
    # that caused the historical ``os.makedirs('')`` failure.  Missing parents are
    # rejected instead of recursively created: creating first and validating later
    # can traverse a symlink/junction and leave directories outside the intended tree.
    lexical_parent = Path(os.path.abspath(path.parent))
    try:
        parent_state = lexical_parent.lstat()
        resolved_parent = lexical_parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ScanStateError(
            f"cannot validate existing output parent directory {path.parent}: {exc}"
        ) from exc
    if (
        lexical_parent.is_symlink()
        or _is_reparse(parent_state)
        or not stat.S_ISDIR(parent_state.st_mode)
        or os.path.normcase(os.path.normpath(str(lexical_parent)))
        != os.path.normcase(os.path.normpath(str(resolved_parent)))
    ):
        raise ScanStateError(
            f"output parent must be a direct real directory: {path.parent}"
        )


def _state_commit_lock_path(root: Path, destination: Path) -> Path:
    """Return one cross-project lock for a physical scan-state target path."""

    lexical = Path(os.path.abspath(destination))
    normalized = os.path.normcase(os.path.normpath(str(lexical))).encode("utf-8")
    digest = hashlib.sha256(_STATE_LOCK_DOMAIN + normalized).hexdigest()
    lock_parent = contract.project_sync_lock_path(root).parent
    return lock_parent / f"scan-state-{digest}.lock"


def write_candidate_state(
    candidate_path: str | os.PathLike[str],
    candidate: Mapping[str, Any],
) -> Path:
    """Atomically write a validated candidate without touching the baseline."""

    data = validate_scan_state_v2(candidate)
    if data["state_role"] != "candidate":
        raise ScanStateError("write_candidate_state requires a candidate state")
    destination = Path(candidate_path)
    _ensure_output_parent(destination)
    _read_optional_regular_bytes(destination, "candidate state")
    return contract.atomic_write_json(destination, data)


def _same_location(first: Path, second: Path) -> bool:
    return os.path.normcase(str(first.resolve(strict=False))) == os.path.normcase(
        str(second.resolve(strict=False))
    )


def _validate_baseline_precondition(
    value: BaselinePrecondition,
) -> BaselinePrecondition:
    if not isinstance(value, BaselinePrecondition):
        raise ScanStateError("commit requires an exact BaselinePrecondition")
    if value.exists:
        if value.sha256 is None:
            raise ScanStateError("existing baseline precondition requires sha256")
        _require_sha256(value.sha256, "expected_baseline.sha256")
        if value.generation_id is not None:
            _require_sha256(
                value.generation_id,
                "expected_baseline.generation_id",
            )
    elif value.sha256 is not None or value.generation_id is not None:
        raise ScanStateError("absent baseline precondition cannot carry hashes")
    return value


def _baseline_matches_bytes(
    expected: BaselinePrecondition,
    current: bytes | None,
) -> bool:
    if expected.exists != (current is not None):
        return False
    if current is None:
        return True
    return hmac.compare_digest(contract.sha256_bytes(current), expected.sha256 or "")


def _baseline_precondition_mapping(
    value: BaselinePrecondition,
) -> dict[str, Any]:
    return {
        "exists": value.exists,
        "sha256": value.sha256,
        "generation_id": value.generation_id,
    }


def _verify_current_baseline_identity(
    current: bytes | None,
    expected: BaselinePrecondition,
    root: Path,
    *,
    source: str,
) -> None:
    if current is None:
        return
    state = parse_previous_state_bytes(current, source=source)
    if state is None:  # pragma: no cover - guarded by current is not None
        raise BaselineConflict("existing baseline parsed as absent")
    actual_generation = None
    if state.get("schema_version") == SCAN_STATE_SCHEMA_VERSION:
        value = state.get("candidate_generation_id")
        actual_generation = value if isinstance(value, str) else None
    if actual_generation != expected.generation_id:
        raise BaselineConflict(
            "committed scan-state generation changed before candidate promotion"
        )
    state_identity = state.get(
        "project_identity",
        state.get("legacy_project_identity"),
    )
    if state_identity is not None and not hmac.compare_digest(
        str(state_identity),
        _project_identity(root),
    ):
        raise BaselineConflict("committed scan state belongs to another project")


def commit_candidate_state(
    candidate_path: str | os.PathLike[str],
    state_path: str | os.PathLike[str],
    *,
    project_root: str | os.PathLike[str],
    expected_baseline: BaselinePrecondition,
    expected_candidate_generation_id: str | None = None,
) -> dict[str, Any]:
    """Promote one candidate only if its project and baseline preimage still match."""

    if expected_candidate_generation_id is None:
        raise ScanStateError("commit requires expected_candidate_generation_id")
    expected_generation = _require_sha256(
        expected_candidate_generation_id,
        "expected_candidate_generation_id",
    )
    baseline = _validate_baseline_precondition(expected_baseline)
    root = _resolved_root(project_root)
    candidate_file = Path(candidate_path)
    destination = Path(state_path)
    if _same_location(candidate_file, destination):
        raise ScanStateError("candidate and committed state paths must be different")
    candidate_bytes = _read_optional_regular_bytes(candidate_file, "candidate state")
    if candidate_bytes is None:
        raise ScanStateError(f"candidate state does not exist: {candidate_file}")
    try:
        candidate_text = candidate_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScanStateError(f"candidate state is not strict UTF-8: {exc}") from exc
    value = contract.parse_json_strict(candidate_text, source=str(candidate_file))
    candidate = validate_scan_state_v2(_require_mapping(value, "candidate scan state"))
    if candidate["state_role"] != "candidate":
        raise ScanStateError("only a candidate state can be committed")
    if not hmac.compare_digest(
        candidate["candidate_generation_id"], expected_generation
    ):
        raise ScanStateError(
            "candidate generation does not match the accepted worklist"
        )
    if not hmac.compare_digest(candidate["project_identity"], _project_identity(root)):
        raise ScanStateError("candidate belongs to a different project root")
    if "baseline_precondition" not in candidate:
        raise ScanStateError(
            "candidate is not bound to an exact committed-state preimage"
        )
    if candidate["baseline_precondition"] != _baseline_precondition_mapping(baseline):
        raise BaselineConflict(
            "candidate baseline precondition does not match the accepted preimage"
        )

    committed = copy.deepcopy(candidate)
    committed["state_role"] = "committed"
    committed["committed_at"] = _now_iso()
    # Committed consumers receive the complete snapshot.  Validation reconstructs
    # the selected candidate worklist before checking candidate_generation_id.
    committed["directories"] = copy.deepcopy(candidate["snapshot"]["directories"])
    committed["total"] = committed["full_total"]
    committed["state_path"] = destination.name
    committed = validate_scan_state_v2(committed)
    expected_bytes = contract.stable_json_dumps(committed).encode("utf-8")

    _ensure_output_parent(destination)
    target_lock = _state_commit_lock_path(root, destination)
    # The project lock serializes normal writers for one root.  A separate lock
    # derived from the physical destination closes the cross-project case where
    # two roots intentionally or accidentally share an external state file.
    with contract.exclusive_file_lock(target_lock):
        with contract.project_sync_lock(root):
            _ensure_output_parent(destination)
            current_bytes = _read_optional_regular_bytes(destination, "scan state")
            if not _baseline_matches_bytes(baseline, current_bytes):
                raise BaselineConflict(
                    "committed scan-state baseline changed before candidate promotion"
                )
            _verify_current_baseline_identity(
                current_bytes,
                baseline,
                root,
                source=str(destination),
            )
            durability_confirmed = False
            try:
                contract.atomic_write_json(destination, committed)
                durability_confirmed = True
                installed = _read_optional_regular_bytes(destination, "scan state")
                if installed != expected_bytes:
                    raise ScanStateError("committed scan state failed exact readback")
            except BaseException as exc:
                try:
                    observed = _read_optional_regular_bytes(destination, "scan state")
                except BaseException:
                    disposition = CommitDisposition.UNKNOWN
                else:
                    if observed == expected_bytes:
                        disposition = CommitDisposition.INSTALLED
                    elif observed == current_bytes and not durability_confirmed:
                        disposition = CommitDisposition.NOT_INSTALLED
                    else:
                        # Once the atomic writer returned successfully, observing the
                        # preimage again is an A→B→A supersession, not evidence that B
                        # was never installed.
                        disposition = CommitDisposition.UNKNOWN
                raise CandidateCommitError(
                    f"candidate commit failed ({disposition.value}): {exc}",
                    disposition=disposition,
                    durability_confirmed=durability_confirmed,
                ) from exc
    return committed


def commit_candidate(
    candidate_path: str | os.PathLike[str],
    state_path: str | os.PathLike[str],
    *,
    project_root: str | os.PathLike[str],
    expected_baseline: BaselinePrecondition,
    expected_candidate_generation_id: str | None = None,
) -> dict[str, Any]:
    """Short alias retained for callers using the approved commit terminology."""

    return commit_candidate_state(
        candidate_path,
        state_path,
        project_root=project_root,
        expected_baseline=expected_baseline,
        expected_candidate_generation_id=expected_candidate_generation_id,
    )


def save_state(
    state_path: str | os.PathLike[str],
    dirs: Sequence[Mapping[str, Any]],
    root: str | os.PathLike[str],
) -> None:
    """Legacy writer: create a candidate at ``state_path`` instead of committing.

    ``dirs`` is retained in the signature for source compatibility.  A fresh scan
    is used so the saved candidate always contains complete schema-v2 snapshots.
    """

    del dirs
    write_candidate_state(state_path, build_candidate_state(root))


def _candidate_path_for(state_path: Path) -> Path:
    suffix = state_path.suffix or ".json"
    stem = state_path.stem if state_path.suffix else state_path.name
    return state_path.with_name(f"{stem}.candidate{suffix}")


def _looks_like_committed_state(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        value = contract.read_json_strict(path)
    except (OSError, contract.ProjectDocsContractError):
        # Existing unknown content is protected as a presumed baseline.
        return True
    return not (
        isinstance(value, Mapping)
        and value.get("schema_version") == SCAN_STATE_SCHEMA_VERSION
        and value.get("state_role") == "candidate"
    )


def _cli_paths(
    args: argparse.Namespace,
    root: Path,
    *,
    commit_generated: bool,
) -> tuple[Path, Path]:
    default_state = root / DEFAULT_STATE_RELATIVE_PATH
    explicit_state = Path(args.state) if args.state else None
    explicit_candidate = Path(args.candidate) if args.candidate else None
    legacy_output = Path(args.output) if args.output else None

    if explicit_state is not None:
        state_path = explicit_state
        candidate_path = (
            explicit_candidate or legacy_output or _candidate_path_for(state_path)
        )
    elif explicit_candidate is not None:
        candidate_path = explicit_candidate
        state_path = legacy_output or default_state
    elif legacy_output is not None:
        # Protect an existing legacy/committed --output file.  For a new path,
        # --output serves as the candidate destination, which is the least
        # surprising behavior for new callers and fixes bare-filename output.
        if commit_generated or _looks_like_committed_state(legacy_output):
            state_path = legacy_output
            candidate_path = _candidate_path_for(state_path)
        else:
            state_path = default_state
            candidate_path = legacy_output
    else:
        state_path = default_state
        candidate_path = _candidate_path_for(state_path)

    if _same_location(candidate_path, state_path):
        raise ScanStateError("candidate and committed state paths must be different")
    return state_path, candidate_path


def _configure_stdout_utf8() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan project directories and produce a schema-v2 candidate state"
    )
    parser.add_argument("root", nargs="?", help="Project root directory")
    parser.add_argument(
        "--output",
        "-o",
        help=(
            "Candidate output for new callers; an existing legacy state is "
            "protected and gets a sibling .candidate.json"
        ),
    )
    parser.add_argument(
        "--state",
        "--state-file",
        dest="state",
        help="Committed baseline path (never replaced unless --commit is explicit)",
    )
    parser.add_argument(
        "--candidate",
        "--candidate-output",
        dest="candidate",
        help="Explicit candidate state path",
    )
    parser.add_argument(
        "--incremental",
        "-i",
        action="store_true",
        help="Return only new/modified work items while snapshotting the full tree",
    )
    parser.add_argument(
        "--full",
        "-f",
        action="store_true",
        help="Force a full scan and ignore the committed baseline",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit strict JSON only (JSON remains the legacy default as well)",
    )
    parser.add_argument(
        "--commit",
        nargs="?",
        const=_COMMIT_GENERATED,
        metavar="CANDIDATE",
        help=(
            "Atomically commit the generated candidate, or commit the supplied "
            "candidate path without scanning"
        ),
    )
    parser.add_argument(
        "--commit-state",
        action="store_true",
        help="Alias for --commit with the generated candidate",
    )
    parser.add_argument(
        "--commit-candidate",
        metavar="CANDIDATE",
        help="Commit an existing candidate without scanning",
    )
    parser.add_argument(
        "--expected-candidate-generation-id",
        metavar="SHA256",
        help=(
            "Required when committing an existing candidate; binds publication "
            "to the candidate generation already processed by the caller"
        ),
    )
    baseline_group = parser.add_mutually_exclusive_group()
    baseline_group.add_argument(
        "--expected-baseline-sha256",
        metavar="SHA256",
        help=(
            "Required for an existing baseline when committing an existing "
            "candidate; this is the preimage hash captured before processing"
        ),
    )
    baseline_group.add_argument(
        "--expected-baseline-absent",
        action="store_true",
        help=(
            "Declare that no committed baseline existed when the existing "
            "candidate was generated"
        ),
    )
    parser.add_argument(
        "--expected-baseline-generation-id",
        metavar="SHA256",
        help="Generation ID captured with --expected-baseline-sha256",
    )
    return parser


def _emit_json(value: Mapping[str, Any]) -> None:
    sys.stdout.write(contract.stable_json_dumps(dict(value)))


def _expected_baseline_from_cli(args: argparse.Namespace) -> BaselinePrecondition:
    if args.expected_baseline_absent:
        if args.expected_baseline_generation_id is not None:
            raise ScanStateError(
                "--expected-baseline-generation-id cannot accompany "
                "--expected-baseline-absent"
            )
        return BaselinePrecondition(False, None, None)
    if args.expected_baseline_sha256 is None:
        raise ScanStateError(
            "committing an existing candidate requires either "
            "--expected-baseline-sha256 or --expected-baseline-absent"
        )
    sha256 = _require_sha256(
        args.expected_baseline_sha256,
        "expected_baseline_sha256",
    )
    generation = args.expected_baseline_generation_id
    if generation is not None:
        generation = _require_sha256(
            generation,
            "expected_baseline_generation_id",
        )
    return BaselinePrecondition(True, sha256, generation)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdout_utf8()
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        commit_existing = args.commit_candidate
        if args.commit not in (None, _COMMIT_GENERATED):
            if commit_existing is not None:
                raise ScanStateError(
                    "--commit CANDIDATE and --commit-candidate are mutually exclusive"
                )
            commit_existing = args.commit

        if commit_existing is not None:
            if not args.root:
                raise ScanStateError(
                    "committing an existing candidate requires the project root"
                )
            state_argument = args.state or args.output
            if not state_argument:
                raise ScanStateError(
                    "committing an existing candidate requires --state (or legacy --output)"
                )
            committed = commit_candidate_state(
                commit_existing,
                state_argument,
                project_root=args.root,
                expected_baseline=_expected_baseline_from_cli(args),
                expected_candidate_generation_id=args.expected_candidate_generation_id,
            )
            _emit_json(committed)
            return 0

        if args.expected_baseline_sha256 is not None or args.expected_baseline_absent:
            raise ScanStateError(
                "expected-baseline options apply only when committing an existing candidate"
            )
        if args.expected_baseline_generation_id is not None:
            raise ScanStateError(
                "--expected-baseline-generation-id requires an existing-candidate commit"
            )
        if not args.root:
            parser.error("the following arguments are required: root")
        root = _resolved_root(args.root)
        commit_generated = args.commit == _COMMIT_GENERATED or args.commit_state
        state_path, candidate_path = _cli_paths(
            args,
            root,
            commit_generated=commit_generated,
        )

        incremental = args.incremental and not args.full
        captured_previous, baseline_precondition, _ = (
            load_previous_state_with_precondition(state_path)
        )
        previous = None if args.full else captured_previous
        candidate = build_candidate_state(
            root,
            previous,
            incremental=incremental,
            force_full=args.full,
            baseline_precondition=baseline_precondition,
        )
        write_candidate_state(candidate_path, candidate)

        output = copy.deepcopy(candidate)
        output["commit"] = {"performed": False}
        if commit_generated:
            expected_generation = (
                args.expected_candidate_generation_id
                or candidate["candidate_generation_id"]
            )
            committed = commit_candidate_state(
                candidate_path,
                state_path,
                project_root=root,
                expected_baseline=baseline_precondition,
                expected_candidate_generation_id=expected_generation,
            )
            output["commit"] = {
                "performed": True,
                "state_path": committed["state_path"],
            }
        _emit_json(output)
        return 0
    except (
        OSError,
        RuntimeError,
        ValueError,
        contract.ProjectDocsContractError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
