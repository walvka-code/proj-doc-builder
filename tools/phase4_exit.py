# -*- coding: utf-8 -*-
"""Fail-closed Phase 4 reporting, quarantine, and receipt restoration.

The default and all legacy ``--auto-approve`` invocations are report-only.
Filesystem changes require either ``--apply`` (quarantine only) or
``--restore RECEIPT_ID``. Documents are never deleted by this tool.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import os
import re
import shutil
import stat
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

import claude_doc_index
import project_docs_contract as contract

AUTO_MARKER = "<!-- AUTO-GENERATED v3.0: folder-doc START -->"
QUARANTINE_DIRNAME = ".quarantine"
RECEIPT_FILENAME = "receipt.json"
PAYLOAD_DIRNAME = "document"
QUARANTINE_REASON = "source-directory-missing-and-zero-inbound-references"

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_OPERATIONAL_FAILURE = 3

#: errno values telling us this kernel/filesystem cannot rename without replacing.
_NO_REPLACE_UNSUPPORTED_ERRNOS = frozenset(
    {
        errno.ENOSYS,
        errno.EINVAL,
        errno.ENOTSUP,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }
)

#: Directories whose durability fsync had to be skipped (filesystem refuses it).
_DEGRADED_DIRECTORY_FSYNC: list[str] = []


class NoReplaceRenameUnsupported(OSError):
    """The platform or filesystem cannot rename atomically without replacing.

    Raised instead of silently falling back to a replacing rename: the publisher
    contract depends on no-replace semantics, so a silent fallback would trade a
    loud failure for a quiet correctness loss.
    """


def directory_fsync_degraded() -> tuple[str, ...]:
    """Return directories whose durability fsync was skipped as unsupported."""

    return tuple(_DEGRADED_DIRECTORY_FSYNC)

_SOURCE_EXCLUSIONS = frozenset(
    {
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
        "project_docs",
        "sub_plan",
        ".hypothesis",
    }
)
_SOURCE_EXCLUSIONS_FOLDED = frozenset(name.casefold() for name in _SOURCE_EXCLUSIONS)
_RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TRANSACTION_SCHEMA_VERSION = 2
_TRANSACTION_KIND = "phase4-transaction"
_TRANSACTION_CANONICAL_FILENAME = ".phase4-transaction.json"
_TRANSACTION_TARGET_FILENAME = ".phase4-transaction-target.json"
_TRANSACTION_READY_FILENAME = ".phase4-transaction-ready.json"
_TRANSACTION_PREFIX = ".phase4-transaction-"
_TRANSACTION_SUFFIX = ".json"
_TRANSACTION_ID_RE = re.compile(r"[a-f0-9]{32}\Z")
_TRANSACTION_LEGACY_RE = re.compile(r"\.phase4-transaction-([a-f0-9]{32})\.json\Z")
_TRANSACTION_TEMP_RE = re.compile(
    r"\.(\.phase4-transaction(?:-target|-ready|-[a-f0-9]{32})?\.json)"
    r"\.publish-([a-f0-9]{32})\.tmp\Z"
)
_MANIFEST_BEFORE_PREFIX = ".phase4-manifest-before-"
_MANIFEST_AFTER_PREFIX = ".phase4-manifest-after-"
_RESTORE_ANCHOR_PREFIX = ".phase4-restore-anchor-"
_DETACHED_PREFIX = ".phase4-detached-"
_ARCHIVE_PREFIX = ".phase4-archive-"
_STAGING_NAME_RE = re.compile(r"\.staging-[A-Za-z0-9_-]{1,200}\Z")
_WINDOWS_RENAME_RETRY_WINERRORS = frozenset((5, 32, 33))
_WINDOWS_RENAME_MAX_ATTEMPTS = 4
_WINDOWS_RENAME_INITIAL_DELAY_SECONDS = 0.01


class Phase4Refusal(Exception):
    """A safety precondition was not established, so no action is allowed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Phase4OperationalError(Exception):
    """A requested filesystem transaction failed operationally."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SourceInventory:
    """Trusted source-directory inventory and boundary/unknown scopes."""

    directories: set[str]
    boundaries: set[str]
    unknown_scopes: tuple[dict[str, str], ...] = ()
    frozen: bool = False


@dataclass
class Analysis:
    """One immutable-in-spirit Phase 4 safety analysis snapshot."""

    root: Path
    docs_root: Path
    manifest: dict[str, Any]
    manifest_entries: dict[str, dict[str, Any]]
    generation: str
    manifest_sha256: str
    active_paths: set[str]
    source_inventory: SourceInventory
    inbound_sources: dict[str, set[str]]
    candidates: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    missing_docs: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def safe(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class ReceiptRecord:
    """已校验的 receipt、payload 与封闭目录身份。"""

    data: dict[str, Any]
    receipt_dir: Path
    payload_path: Path
    directory_identity: tuple[int, int, int, int]
    payload_identity: tuple[int, int, int, int] | None


@dataclass
class QuarantineMove:
    """尚未提交的多文档 quarantine 批事务成员。"""

    receipt: dict[str, Any]
    source_path: Path
    staging_dir: Path
    final_dir: Path
    staging_payload: Path
    final_payload: Path
    moved: bool = False
    finalized: bool = False

    @property
    def payload_path(self) -> Path:
        return self.final_payload if self.finalized else self.staging_payload


class TextUpdateState(str, Enum):
    """一次原子文本发布的可证明处置状态。"""

    NOT_ATTEMPTED = "not-attempted"
    BEFORE = "before"
    AFTER = "after"
    UNKNOWN = "unknown"


class ManifestCommitDisposition(str, Enum):
    """Manifest commit point 的互斥耐久处置状态。"""

    DURABLE = "durable"
    INSTALLED_DURABILITY_UNKNOWN = "installed-durability-unknown"


@dataclass
class TextUpdate:
    """可在 commit point 前回滚的原子文本替换。"""

    path: Path
    before: str
    after: str
    archive_root: Path | None = None
    archive_trusted_root: Path | None = None
    state: TextUpdateState = TextUpdateState.NOT_ATTEMPTED


@dataclass(frozen=True)
class ExclusiveCopy:
    """本进程排他发布的目标文件身份及安全 archive 边界。"""

    path: Path
    identity: tuple[int, int, int, int]
    archive_root: Path | None = None
    archive_trusted_root: Path | None = None
    snapshot: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TransactionJournal:
    """已验证的持久 Phase 4 事务日志。"""

    path: Path
    quarantine_root: Path
    data: dict[str, Any]
    file_snapshot: dict[str, Any]


def _is_link_or_junction(path: Path) -> bool:
    """Recognize symbolic links and Windows junction/reparse directory links."""

    try:
        if path.is_symlink():
            return True
        path_is_junction = getattr(path, "is_junction", None)
        if path_is_junction is not None and path_is_junction():
            return True
        os_is_junction = getattr(os.path, "isjunction", None)
        return bool(os_is_junction is not None and os_is_junction(os.fspath(path)))
    except OSError as exc:
        raise Phase4Refusal(
            "untrusted-path",
            f"cannot inspect link or junction state for {path}: {exc}",
        ) from exc


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _fsync_directory(directory: Path) -> None:
    """Persist one trusted directory namespace entry on supported platforms."""

    directory = Path(directory)
    if not _lexists(directory) or _is_link_or_junction(directory):
        raise OSError(
            f"directory durability target is missing or indirect: {directory}"
        )
    before = directory.lstat()
    if not stat.S_ISDIR(before.st_mode):
        raise OSError(f"directory durability target is not a directory: {directory}")
    expected_identity = _identity_from_stat(before)

    if os.name == "nt":
        _fsync_directory_windows(directory, expected_identity)
    elif os.name == "posix":
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(directory, flags)
        try:
            opened = os.fstat(fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _identity_from_stat(opened) != expected_identity
            ):
                raise OSError(
                    f"directory identity changed while opening durability handle: {directory}"
                )
            try:
                os.fsync(fd)
            except OSError as exc:
                # Some filesystems (and a few network mounts) refuse to fsync a
                # directory. Degrade loudly instead of making the whole publisher
                # unusable there; real I/O errors still propagate.
                if exc.errno not in _NO_REPLACE_UNSUPPORTED_ERRNOS:
                    raise
                entry = f"{directory}: {exc.strerror or exc}"
                if entry not in _DEGRADED_DIRECTORY_FSYNC:
                    _DEGRADED_DIRECTORY_FSYNC.append(entry)
                warnings.warn(
                    "directory fsync is not supported here; durability is "
                    f"degraded for {entry}",
                    RuntimeWarning,
                    stacklevel=2,
                )
        finally:
            os.close(fd)
    else:  # pragma: no cover - unsupported Python platform
        raise OSError(
            f"directory durability is unsupported on platform {os.name!r}: {directory}"
        )

    if (
        not _lexists(directory)
        or _is_link_or_junction(directory)
        or _identity_from_stat(directory.lstat()) != expected_identity
    ):
        raise OSError(
            f"directory identity changed during durability flush: {directory}"
        )


def _fsync_directory_windows(
    directory: Path,
    expected_identity: tuple[int, int, int, int],
) -> None:
    """Flush an NT directory handle and bind it to the inspected path identity."""

    import msvcrt

    generic_write = 0x40000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    invalid_handle_value = ctypes.c_void_p(-1).value

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [ctypes.c_void_p]
    flush_file_buffers.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = create_file(
        os.fspath(directory),
        generic_write,
        share_all,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle in (None, invalid_handle_value):
        raise ctypes.WinError(ctypes.get_last_error())

    fd = -1
    try:
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            close_handle(handle)
            raise
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _identity_from_stat(opened) != expected_identity
        ):
            raise OSError(
                f"Windows directory handle identity differs from its path: {directory}"
            )
        raw_handle = msvcrt.get_osfhandle(fd)
        if not flush_file_buffers(raw_handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if fd >= 0:
            os.close(fd)


def _fsync_regular_file(
    path: Path,
    expected_identity: tuple[int, int, int, int],
    description: str,
) -> None:
    """Flush one no-follow regular file while binding the handle to its identity."""

    access = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    flags = access | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _identity_from_stat(opened) != expected_identity
        ):
            raise OSError(
                f"{description} identity changed before durability flush: {path}"
            )
        os.fsync(fd)
    finally:
        if fd >= 0:
            os.close(fd)
    if (
        not _lexists(path)
        or _is_link_or_junction(path)
        or _identity_from_stat(path.lstat()) != expected_identity
    ):
        raise OSError(f"{description} identity changed during durability flush: {path}")


def _require_below(candidate: Path, parent: Path, description: str) -> None:
    try:
        candidate.relative_to(parent)
    except ValueError as exc:
        raise Phase4Refusal(
            "untrusted-path",
            f"{description} resolves outside its trusted root: {candidate}",
        ) from exc


def _validate_existing_components(base: Path, target: Path, description: str) -> None:
    """Reject links/junctions in all existing components below ``base``."""

    _require_below(target, base, description)
    relative = target.relative_to(base)
    current = base
    for part in relative.parts:
        current = current / part
        if _lexists(current) and _is_link_or_junction(current):
            raise Phase4Refusal(
                "untrusted-path",
                f"{description} contains a link or junction: {current}",
            )


def _resolve_project(root_value: str | os.PathLike[str]) -> tuple[Path, Path]:
    supplied_root = Path(root_value)
    try:
        root = supplied_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "project-root-invalid",
            f"project root cannot be resolved: {supplied_root}",
        ) from exc
    if not root.is_dir():
        raise Phase4Refusal(
            "project-root-invalid",
            f"project root is not a directory: {root}",
        )

    lexical_docs_root = root / "project_docs"
    if _is_link_or_junction(lexical_docs_root):
        raise Phase4Refusal(
            "untrusted-path",
            f"project_docs must not be a link or junction: {lexical_docs_root}",
        )
    try:
        docs_root = lexical_docs_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "project-docs-missing",
            f"project_docs cannot be resolved: {lexical_docs_root}",
        ) from exc
    if not docs_root.is_dir():
        raise Phase4Refusal(
            "project-docs-missing",
            f"project_docs is not a directory: {docs_root}",
        )
    _require_below(docs_root, root, "project_docs")
    if docs_root.parent != root:
        raise Phase4Refusal(
            "untrusted-path",
            "project_docs must be a direct, real child of the project root",
        )
    _validate_existing_quarantine(docs_root)
    return root, docs_root


def _is_quarantine_relative(relative_path: str) -> bool:
    parts = PurePosixPath(relative_path).parts
    return bool(parts and parts[0].casefold() == QUARANTINE_DIRNAME.casefold())


def _validate_existing_quarantine(docs_root: Path) -> Path | None:
    quarantine_root = docs_root / QUARANTINE_DIRNAME
    if not _lexists(quarantine_root):
        return None
    if _is_link_or_junction(quarantine_root):
        raise Phase4Refusal(
            "untrusted-quarantine",
            f"quarantine root must not be a link or junction: {quarantine_root}",
        )
    try:
        resolved = quarantine_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "untrusted-quarantine",
            f"quarantine root cannot be resolved: {quarantine_root}",
        ) from exc
    _require_below(resolved, docs_root, "quarantine root")
    if not resolved.is_dir() or resolved.parent != docs_root:
        raise Phase4Refusal(
            "untrusted-quarantine",
            f"quarantine root is not a direct directory under project_docs: {resolved}",
        )
    return resolved


def _prepare_quarantine_root(docs_root: Path) -> Path:
    existing = _validate_existing_quarantine(docs_root)
    if existing is not None:
        return existing
    quarantine_root = docs_root / QUARANTINE_DIRNAME
    try:
        _ensure_directory_chain(docs_root, quarantine_root, "quarantine root")
    except Phase4Refusal:
        raise
    except Phase4OperationalError as exc:
        raise Phase4OperationalError(
            "quarantine-create-failed",
            f"cannot create quarantine root {quarantine_root}: {exc.message}",
        ) from exc
    return _validate_existing_quarantine(docs_root) or quarantine_root


def _absent_snapshot() -> dict[str, Any]:
    """Return the closed-schema representation of a known absent text file."""

    return {"exists": False, "sha256": None, "text": None}


def _text_snapshot(text: str) -> dict[str, Any]:
    """Capture strict UTF-8 text and its exact SHA-256 in a journal snapshot."""

    if not isinstance(text, str):
        raise Phase4Refusal(
            "recovery-journal-invalid", "journal text snapshot is not text"
        )
    return {
        "exists": True,
        "sha256": contract.sha256_bytes(text.encode("utf-8")),
        "text": text,
    }


def _validate_snapshot(value: Any, field_name: str) -> dict[str, Any]:
    """Validate one closed-schema journal text snapshot."""

    if not isinstance(value, Mapping) or set(value) != {"exists", "sha256", "text"}:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name} must be a closed text snapshot",
        )
    exists = value.get("exists")
    if type(exists) is not bool:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name}.exists must be boolean",
        )
    text = value.get("text")
    digest = value.get("sha256")
    if not exists:
        if text is not None or digest is not None:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                f"{field_name} absent snapshot must have null text and hash",
            )
        return _absent_snapshot()
    if not isinstance(text, str) or not isinstance(digest, str):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name} present snapshot must contain text and SHA-256",
        )
    if _SHA256_RE.fullmatch(digest) is None:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name} has an invalid SHA-256",
        )
    expected = contract.sha256_bytes(text.encode("utf-8"))
    if digest != expected:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name} text and SHA-256 disagree",
        )
    return _text_snapshot(text)


def _open_regular_file_no_follow(path: Path) -> int:
    """Open one regular file without following its final path component."""

    if os.name != "nt":
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:  # pragma: no cover - unsupported POSIX runtime
            raise OSError("O_NOFOLLOW is unavailable")
        flags = (
            os.O_RDONLY
            | no_follow
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        return os.open(path, flags)

    import msvcrt

    generic_read = 0x80000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_attribute_reparse_point = 0x00000400
    file_flag_open_reparse_point = 0x00200000
    file_attribute_tag_info = 9
    invalid_handle_value = ctypes.c_void_p(-1).value

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", ctypes.c_uint32),
            ("reparse_tag", ctypes.c_uint32),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_file_information = kernel32.GetFileInformationByHandleEx
    get_file_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    get_file_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = create_file(
        str(path),
        generic_read,
        share_all,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle in (None, invalid_handle_value):
        raise ctypes.WinError(ctypes.get_last_error())
    info = FileAttributeTagInfo()
    if not get_file_information(
        handle,
        file_attribute_tag_info,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error = ctypes.WinError(ctypes.get_last_error())
        close_handle(handle)
        raise error
    if info.file_attributes & file_attribute_reparse_point:
        close_handle(handle)
        raise OSError(f"refusing a Windows reparse-point file: {path}")
    try:
        return msvcrt.open_osfhandle(
            handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
    except BaseException:
        close_handle(handle)
        raise


def _read_stable_regular_fd(
    fd: int,
    path: Path,
) -> tuple[bytes, os.stat_result]:
    """Read one opened regular file twice and reject an unstable handle."""

    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise OSError(f"opened path is not a regular file: {path}")

    def read_once() -> bytes:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    first = read_once()
    middle = os.fstat(fd)
    second = read_once()
    after = os.fstat(fd)
    identity = _identity_from_stat(before)
    stable_metadata = (
        int(before.st_size),
        int(getattr(before, "st_mtime_ns", 0)),
        int(getattr(before, "st_ctime_ns", 0)),
    )
    for observed in (middle, after):
        if (
            not stat.S_ISREG(observed.st_mode)
            or _identity_from_stat(observed) != identity
            or (
                int(observed.st_size),
                int(getattr(observed, "st_mtime_ns", 0)),
                int(getattr(observed, "st_ctime_ns", 0)),
            )
            != stable_metadata
        ):
            raise OSError(f"opened file changed while reading it: {path}")
    if first != second or len(first) != int(after.st_size):
        raise OSError(f"opened file content changed while reading it: {path}")
    return first, after


def _capture_snapshot_with_identity(
    path: Path,
    base: Path,
    description: str,
) -> tuple[dict[str, Any], tuple[int, int, int, int] | None]:
    """Capture strict UTF-8 bytes and identity from one no-follow file handle."""

    _validate_existing_components(base, path, description)
    if not _lexists(path):
        return _absent_snapshot(), None
    if _is_link_or_junction(path):
        raise Phase4Refusal(
            "untrusted-path",
            f"{description} must be a trusted regular file: {path}",
        )

    fd = -1
    try:
        fd = _open_regular_file_no_follow(path)
        raw, opened = _read_stable_regular_fd(fd, path)
        identity = _identity_from_stat(opened)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise contract.StrictUtf8Error(f"invalid UTF-8 in {path}: {exc}") from exc
        snapshot = _text_snapshot(text)
    except Phase4Refusal:
        raise
    except (OSError, contract.ProjectDocsContractError) as exc:
        raise Phase4Refusal(
            "untrusted-path",
            f"{description} cannot be read as a stable strict UTF-8 file: "
            f"{path}: {exc}",
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)

    try:
        after = path.lstat()
    except OSError as exc:
        raise Phase4Refusal(
            "untrusted-path",
            f"{description} path changed after capturing its snapshot: {path}: {exc}",
        ) from exc
    if (
        _is_link_or_junction(path)
        or not stat.S_ISREG(after.st_mode)
        or _identity_from_stat(after) != identity
    ):
        raise Phase4Refusal(
            "untrusted-path",
            f"{description} identity changed while capturing its snapshot: {path}",
        )
    return snapshot, identity


def _capture_snapshot(path: Path, base: Path, description: str) -> dict[str, Any]:
    """Read one trusted optional regular file into a journal snapshot."""

    snapshot, _ = _capture_snapshot_with_identity(path, base, description)
    return snapshot


def _snapshot_matches(
    path: Path, base: Path, description: str, snapshot: Mapping[str, Any]
) -> bool:
    """Compare the current trusted path state with an already validated snapshot."""

    return _capture_snapshot(path, base, description) == dict(snapshot)


def _require_snapshot_state(
    path: Path,
    base: Path,
    description: str,
    snapshot: Mapping[str, Any],
    *,
    conflict_code: str = "recovery-conflict",
) -> None:
    if not _snapshot_matches(path, base, description, snapshot):
        raise Phase4Refusal(
            conflict_code,
            f"{description} differs from the transaction journal: {path}",
        )


def _legacy_journal_filename(transaction_id: str) -> str:
    if _TRANSACTION_ID_RE.fullmatch(transaction_id) is None:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"transaction ID is invalid: {transaction_id!r}",
        )
    return f"{_TRANSACTION_PREFIX}{transaction_id}{_TRANSACTION_SUFFIX}"


def _journal_filename(transaction_id: str) -> str:
    """Return the project-wide singleton journal slot for new transactions."""

    _legacy_journal_filename(transaction_id)
    return _TRANSACTION_CANONICAL_FILENAME


def _transaction_journal_path(quarantine_root: Path, transaction_id: str) -> Path:
    path = quarantine_root / _journal_filename(transaction_id)
    _require_below(path, quarantine_root, "transaction journal")
    return path


def _transaction_target_path(quarantine_root: Path) -> Path:
    return quarantine_root / _TRANSACTION_TARGET_FILENAME


def _transaction_ready_path(quarantine_root: Path) -> Path:
    return quarantine_root / _TRANSACTION_READY_FILENAME


def _manifest_transaction_paths(
    docs_root: Path,
    transaction_id: str,
) -> tuple[Path, Path]:
    if _TRANSACTION_ID_RE.fullmatch(transaction_id) is None:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"transaction ID is invalid: {transaction_id!r}",
        )
    return (
        docs_root / f"{_MANIFEST_BEFORE_PREFIX}{transaction_id}.tmp",
        docs_root / f"{_MANIFEST_AFTER_PREFIX}{transaction_id}.tmp",
    )


def _restore_anchor_path(quarantine_root: Path, receipt_id: str) -> Path:
    _validate_receipt_id(receipt_id)
    return quarantine_root / f"{_RESTORE_ANCHOR_PREFIX}{receipt_id}.tmp"


def _journal_path_matches_transaction(path: Path, transaction_id: str) -> bool:
    """Accept the fixed slot, legacy v1 slots, and owned publication temps."""

    if path.name in (
        _TRANSACTION_CANONICAL_FILENAME,
        _TRANSACTION_TARGET_FILENAME,
        _TRANSACTION_READY_FILENAME,
    ):
        return True
    legacy = _TRANSACTION_LEGACY_RE.fullmatch(path.name)
    if legacy is not None:
        return legacy.group(1) == transaction_id
    temporary = _TRANSACTION_TEMP_RE.fullmatch(path.name)
    if temporary is None:
        return False
    target_name = temporary.group(1)
    legacy_target = _TRANSACTION_LEGACY_RE.fullmatch(target_name)
    return legacy_target is None or legacy_target.group(1) == transaction_id


def _validate_staging_name(value: Any) -> str:
    if not isinstance(value, str) or _STAGING_NAME_RE.fullmatch(value) is None:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "transaction staging directory name is invalid",
        )
    return value


def _journal_snapshot_after(value: Any, field_name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    return _validate_snapshot(value, field_name)


def _validate_journal_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"before", "after"}:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "journal manifest state must contain before and after snapshots",
        )
    before = _validate_snapshot(value.get("before"), "manifest.before")
    if not before["exists"]:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "manifest.before must exist",
        )
    after = _journal_snapshot_after(value.get("after"), "manifest.after")
    if after is not None and not after["exists"]:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "manifest.after must exist when prepared",
        )
    return {"before": before, "after": after}


def _validate_journal_claude(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"before", "after"}:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "journal CLAUDE state must contain before and after snapshots",
        )
    return {
        "before": _validate_snapshot(value.get("before"), "claude.before"),
        "after": _journal_snapshot_after(value.get("after"), "claude.after"),
    }


def _receipt_snapshot_data(
    snapshot: Mapping[str, Any],
    *,
    receipt_id: str,
    original_path: str,
    field_name: str,
) -> dict[str, Any]:
    """Validate a receipt text snapshot against its immutable identifiers."""

    if not snapshot["exists"]:
        return {}
    try:
        parsed = contract.parse_json_strict(snapshot["text"], source=field_name)
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("recovery-journal-invalid", str(exc)) from exc
    if not isinstance(parsed, Mapping):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name} receipt JSON root must be an object",
        )
    receipt = _validate_receipt_data(dict(parsed), receipt_id)
    if receipt["original_path"] != original_path:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{field_name} receipt original_path differs from the transaction path",
        )
    return receipt


def _validate_quarantine_journal(data: Mapping[str, Any]) -> dict[str, Any]:
    if set(data) != {
        "claude",
        "created_at",
        "kind",
        "manifest",
        "operation",
        "phase",
        "quarantine",
        "schema_version",
        "transaction_id",
    }:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "quarantine journal has unsupported fields",
        )
    quarantine = data.get("quarantine")
    if not isinstance(quarantine, Mapping) or set(quarantine) != {"moves"}:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "quarantine journal must contain its move list",
        )
    raw_moves = quarantine.get("moves")
    if not isinstance(raw_moves, list) or not raw_moves:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "quarantine journal move list must be non-empty",
        )
    moves: list[dict[str, Any]] = []
    receipt_ids: set[str] = set()
    originals: set[str] = set()
    staging_names: set[str] = set()
    for index, raw_move in enumerate(raw_moves):
        if not isinstance(raw_move, Mapping) or set(raw_move) != {
            "original_path",
            "receipt_after",
            "receipt_before",
            "receipt_id",
            "source_before",
            "staging_name",
        }:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                f"quarantine.moves[{index}] has unsupported fields",
            )
        receipt_id = raw_move.get("receipt_id")
        _validate_receipt_id(receipt_id)
        original = raw_move.get("original_path")
        try:
            original = contract.validate_manifest_relative_path(original).as_posix()
        except contract.PathValidationError as exc:
            raise Phase4Refusal("recovery-journal-invalid", str(exc)) from exc
        if _is_quarantine_relative(original):
            raise Phase4Refusal(
                "recovery-journal-invalid",
                "quarantine source path cannot point into quarantine",
            )
        source_before = _validate_snapshot(
            raw_move.get("source_before"),
            f"quarantine.moves[{index}].source_before",
        )
        receipt_before = _validate_snapshot(
            raw_move.get("receipt_before"),
            f"quarantine.moves[{index}].receipt_before",
        )
        receipt_after = _validate_snapshot(
            raw_move.get("receipt_after"),
            f"quarantine.moves[{index}].receipt_after",
        )
        if (
            not source_before["exists"]
            or receipt_before["exists"]
            or not receipt_after["exists"]
        ):
            raise Phase4Refusal(
                "recovery-journal-invalid",
                f"quarantine.moves[{index}] has invalid before/after presence",
            )
        receipt_data = _receipt_snapshot_data(
            receipt_after,
            receipt_id=receipt_id,
            original_path=original,
            field_name=f"quarantine.moves[{index}].receipt_after",
        )
        if receipt_data.get("sha256") != source_before["sha256"]:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                f"quarantine.moves[{index}] receipt hash differs from source snapshot",
            )
        _receipt_payload_identity(
            receipt_data,
            error_code="recovery-journal-invalid",
            context=f"quarantine.moves[{index}] receipt",
        )
        staging_name = _validate_staging_name(raw_move.get("staging_name"))
        if (
            receipt_id in receipt_ids
            or original in originals
            or staging_name in staging_names
        ):
            raise Phase4Refusal(
                "recovery-journal-invalid",
                "quarantine journal contains duplicate move identities",
            )
        receipt_ids.add(receipt_id)
        originals.add(original)
        staging_names.add(staging_name)
        moves.append(
            {
                "receipt_id": receipt_id,
                "original_path": original,
                "source_before": source_before,
                "receipt_before": receipt_before,
                "receipt_after": receipt_after,
                "staging_name": staging_name,
            }
        )
    return {"moves": moves}


def _validate_restore_journal(data: Mapping[str, Any]) -> dict[str, Any]:
    if set(data) != {
        "claude",
        "created_at",
        "kind",
        "manifest",
        "operation",
        "phase",
        "restore",
        "schema_version",
        "transaction_id",
    }:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore journal has unsupported fields",
        )
    raw_restore = data.get("restore")
    required = {
        "original_path",
        "payload_after",
        "payload_before",
        "receipt_after",
        "receipt_before",
        "receipt_id",
        "target_after",
        "target_before",
        "target_identity",
    }
    if not isinstance(raw_restore, Mapping) or set(raw_restore) != required:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore journal has unsupported fields",
        )
    receipt_id = raw_restore.get("receipt_id")
    _validate_receipt_id(receipt_id)
    try:
        original = contract.validate_manifest_relative_path(
            raw_restore.get("original_path")
        ).as_posix()
    except contract.PathValidationError as exc:
        raise Phase4Refusal("recovery-journal-invalid", str(exc)) from exc
    if _is_quarantine_relative(original):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore target cannot point into quarantine",
        )
    target_before = _validate_snapshot(
        raw_restore.get("target_before"), "restore.target_before"
    )
    target_after = _validate_snapshot(
        raw_restore.get("target_after"), "restore.target_after"
    )
    phase = data.get("phase")
    target_identity: tuple[int, int, int, int] | None
    if phase == "prepared":
        if raw_restore.get("target_identity") is not None:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                "prepared restore journal cannot claim a target identity",
            )
        target_identity = None
    else:
        try:
            target_identity = _validate_serialized_identity(
                raw_restore.get("target_identity"),
                "restore.target_identity",
            )
        except Phase4Refusal as exc:
            raise Phase4Refusal("recovery-journal-invalid", exc.message) from exc
    receipt_before = _validate_snapshot(
        raw_restore.get("receipt_before"), "restore.receipt_before"
    )
    receipt_after = _validate_snapshot(
        raw_restore.get("receipt_after"), "restore.receipt_after"
    )
    payload_before = _validate_snapshot(
        raw_restore.get("payload_before"), "restore.payload_before"
    )
    payload_after = _validate_snapshot(
        raw_restore.get("payload_after"), "restore.payload_after"
    )
    if (
        target_before["exists"]
        or not target_after["exists"]
        or not receipt_before["exists"]
        or not receipt_after["exists"]
        or not payload_before["exists"]
        or payload_after["exists"]
    ):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore journal has invalid before/after presence",
        )
    before_receipt = _receipt_snapshot_data(
        receipt_before,
        receipt_id=receipt_id,
        original_path=original,
        field_name="restore.receipt_before",
    )
    after_receipt = _receipt_snapshot_data(
        receipt_after,
        receipt_id=receipt_id,
        original_path=original,
        field_name="restore.receipt_after",
    )
    if (
        before_receipt.get("state") != "quarantined"
        or after_receipt.get("state") != "restored"
    ):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore receipt states must transition quarantined to restored",
        )
    before_payload_identity = _receipt_payload_identity(
        before_receipt,
        error_code="recovery-journal-invalid",
        context="restore receipt.before",
    )
    after_payload_identity = _receipt_payload_identity(
        after_receipt,
        error_code="recovery-journal-invalid",
        context="restore receipt.after",
    )
    if before_payload_identity != after_payload_identity:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore receipt payload identity changes across the transaction",
        )
    if target_identity is not None and target_identity == before_payload_identity:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore target identity aliases the quarantine payload identity",
        )
    if not (
        target_after["sha256"]
        == payload_before["sha256"]
        == before_receipt.get("sha256")
        and after_receipt.get("sha256") == before_receipt.get("sha256")
        and target_after["text"] == payload_before["text"]
    ):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore target, payload, and receipt hashes disagree",
        )
    return {
        "receipt_id": receipt_id,
        "original_path": original,
        "target_before": target_before,
        "target_after": target_after,
        "target_identity": (None if target_identity is None else list(target_identity)),
        "receipt_before": receipt_before,
        "receipt_after": receipt_after,
        "payload_before": payload_before,
        "payload_after": payload_after,
    }


def _validate_transaction_journal(
    value: Any,
    path: Path,
    quarantine_root: Path,
) -> TransactionJournal:
    """Strictly parse a journal before using any embedded path or text state."""

    if not isinstance(value, Mapping):
        raise Phase4Refusal(
            "recovery-journal-invalid", "journal JSON root must be an object"
        )
    data = dict(value)
    schema_version = data.get("schema_version")
    if schema_version == 1:
        raise Phase4Refusal(
            "recovery-journal-legacy-schema",
            "Phase 4 transaction schema 1 lacks the persisted identity and immutable "
            "generation invariants required for safe automatic recovery; retain the "
            "journal for audited manual recovery",
        )
    if (
        schema_version != _TRANSACTION_SCHEMA_VERSION
        or data.get("kind") != _TRANSACTION_KIND
    ):
        raise Phase4Refusal(
            "recovery-journal-invalid", "unsupported transaction journal schema"
        )
    transaction_id = data.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
    ):
        raise Phase4Refusal(
            "recovery-journal-invalid", "journal transaction_id is invalid"
        )
    if not _journal_path_matches_transaction(path, transaction_id):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "journal filename does not match its transaction_id",
        )
    _validate_timestamp(
        data.get("created_at"),
        "journal created_at",
        error_code="recovery-journal-invalid",
    )
    phase = data.get("phase")
    if phase not in ("prepared", "target-created", "ready"):
        raise Phase4Refusal("recovery-journal-invalid", "journal phase is unsupported")
    operation = data.get("operation")
    if operation not in ("quarantine", "restore"):
        raise Phase4Refusal(
            "recovery-journal-invalid", "journal operation is unsupported"
        )
    if phase == "target-created" and operation != "restore":
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "only restore transactions may use the target-created phase",
        )
    manifest = _validate_journal_manifest(data.get("manifest"))
    claude = _validate_journal_claude(data.get("claude"))
    if phase == "ready" and (manifest["after"] is None or claude["after"] is None):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "ready journal lacks an after manifest or CLAUDE state",
        )
    if phase != "ready" and (
        manifest["after"] is not None or claude["after"] is not None
    ):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{phase} journal already contains published metadata",
        )
    normalized: dict[str, Any] = {
        "schema_version": _TRANSACTION_SCHEMA_VERSION,
        "kind": _TRANSACTION_KIND,
        "operation": operation,
        "transaction_id": transaction_id,
        "created_at": data["created_at"],
        "phase": phase,
        "manifest": manifest,
        "claude": claude,
    }
    if operation == "quarantine":
        normalized["quarantine"] = _validate_quarantine_journal(data)
    else:
        normalized["restore"] = _validate_restore_journal(data)
    return TransactionJournal(
        path=path,
        quarantine_root=quarantine_root,
        data=normalized,
        file_snapshot=_text_snapshot(contract.stable_json_dumps(normalized)),
    )


def _load_transaction_journal(path: Path, quarantine_root: Path) -> TransactionJournal:
    _validate_existing_components(quarantine_root, path, "transaction journal")
    if _is_link_or_junction(path) or not path.is_file():
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"transaction journal is not a trusted regular file: {path}",
        )
    try:
        text = contract.read_utf8(path)
        value = contract.parse_json_strict(text, source=str(path))
    except (OSError, contract.ProjectDocsContractError) as exc:
        raise Phase4Refusal("recovery-journal-invalid", str(exc)) from exc
    journal = _validate_transaction_journal(value, path, quarantine_root)
    return TransactionJournal(
        path=journal.path,
        quarantine_root=journal.quarantine_root,
        data=journal.data,
        file_snapshot=_text_snapshot(text),
    )


def _require_target_journal_transition(
    prepared: TransactionJournal,
    target_created: TransactionJournal,
) -> None:
    """Bind a restore target-created companion to its immutable prepared intent."""

    if (
        prepared.data["phase"] != "prepared"
        or prepared.data["operation"] != "restore"
        or target_created.data["phase"] != "target-created"
    ):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "restore target companion has an invalid phase transition",
        )
    stable_fields = (
        "schema_version",
        "kind",
        "operation",
        "transaction_id",
        "created_at",
    )
    if any(
        prepared.data[field] != target_created.data[field] for field in stable_fields
    ):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "restore target companion does not belong to its prepared journal",
        )
    expected_restore = dict(prepared.data["restore"])
    expected_restore["target_identity"] = target_created.data["restore"][
        "target_identity"
    ]
    if (
        prepared.data["restore"]["target_identity"] is not None
        or expected_restore != target_created.data["restore"]
        or prepared.data["manifest"] != target_created.data["manifest"]
        or prepared.data["claude"] != target_created.data["claude"]
    ):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "restore target companion changes immutable prepared state",
        )


def _require_ready_journal_transition(
    previous: TransactionJournal,
    ready: TransactionJournal,
) -> None:
    """Bind an immutable ready companion to its required prior generation."""

    operation = previous.data["operation"]
    expected_phase = "prepared" if operation == "quarantine" else "target-created"
    if previous.data["phase"] != expected_phase or ready.data["phase"] != "ready":
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "transaction ready companion has an invalid phase transition",
        )
    stable_fields = (
        "schema_version",
        "kind",
        "operation",
        "transaction_id",
        "created_at",
    )
    if any(previous.data[field] != ready.data[field] for field in stable_fields):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "transaction ready companion does not belong to its prior journal",
        )
    if (
        previous.data[operation] != ready.data[operation]
        or previous.data["manifest"]["before"] != ready.data["manifest"]["before"]
        or previous.data["claude"]["before"] != ready.data["claude"]["before"]
        or ready.data["manifest"]["after"] is None
        or ready.data["claude"]["after"] is None
    ):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "transaction ready companion changes immutable prior state",
        )


def _journal_entry_identity(path: Path, description: str) -> tuple[int, int, int, int]:
    if _is_link_or_junction(path):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{description} is a link or junction: {path}",
        )
    try:
        info = path.lstat()
    except OSError as exc:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"cannot inspect {description}: {path}: {exc}",
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"{description} is not a regular file: {path}",
        )
    return _identity_from_stat(info)


def _journal_snapshot_identity(
    journal: TransactionJournal,
    description: str,
) -> tuple[int, int, int, int]:
    snapshot, identity = _capture_snapshot_with_identity(
        journal.path,
        journal.quarantine_root,
        description,
    )
    if snapshot != journal.file_snapshot or identity is None:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"{description} changed after validation: {journal.path}",
        )
    return identity


def _prepared_journal_paths(quarantine_root: Path) -> list[Path]:
    """Enumerate every fixed or legacy prepared journal slot."""

    try:
        entries = sorted(quarantine_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"cannot enumerate prepared transaction journals: {exc}",
        ) from exc
    return [
        entry
        for entry in entries
        if entry.name == _TRANSACTION_CANONICAL_FILENAME
        or _TRANSACTION_LEGACY_RE.fullmatch(entry.name) is not None
    ]


def _promote_publication_temp(
    temporary: TransactionJournal,
    quarantine_root: Path,
) -> TransactionJournal:
    """Promote one fully validated publication temp into the fixed journal slot."""

    match = _TRANSACTION_TEMP_RE.fullmatch(temporary.path.name)
    if match is None:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"transaction journal publication temp has an invalid name: {temporary.path}",
        )
    canonical = quarantine_root / match.group(1)
    if _lexists(canonical):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"transaction journal target appeared during temp recovery: {canonical}",
        )
    temporary_identity = _journal_snapshot_identity(
        temporary,
        "transaction journal publication temp",
    )
    try:
        # A process may have died after writing valid bytes but before fsync.  Bind
        # and flush the exact temp inode before making its name canonical.
        _fsync_regular_file(
            temporary.path,
            temporary_identity,
            "transaction journal publication temp",
        )
        _rename_no_replace(temporary.path, canonical)
    except FileExistsError as exc:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"canonical transaction journal appeared during temp recovery: {canonical}",
        ) from exc
    except OSError as exc:
        raise Phase4OperationalError(
            "recovery-journal-temp-promote-failed",
            f"cannot promote transaction journal publication temp: {exc}",
        ) from exc
    if (
        _journal_entry_identity(canonical, "canonical transaction journal")
        != temporary_identity
    ):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "promoted transaction journal identity differs from its publication temp",
        )
    _require_snapshot_state(
        canonical,
        quarantine_root,
        "canonical transaction journal",
        temporary.file_snapshot,
        conflict_code="recovery-journal-conflict",
    )
    _fsync_directory(quarantine_root)
    return _load_transaction_journal(canonical, quarantine_root)


def _pending_transaction(
    root_value: str | os.PathLike[str],
    *,
    recover_publication_temp: bool = False,
) -> TransactionJournal | None:
    """Return one immutable journal generation and fail closed on ambiguity."""

    _, docs_root = _resolve_project(root_value)
    quarantine_root = _validate_existing_quarantine(docs_root)
    if quarantine_root is None:
        return None
    try:
        entries = sorted(quarantine_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"cannot enumerate quarantine journals: {exc}",
        ) from exc

    prepared_paths: list[Path] = []
    target_paths: list[Path] = []
    ready_paths: list[Path] = []
    publication_temps: list[Path] = []
    detached: list[Path] = []
    for entry in entries:
        name = entry.name
        is_target = name == _TRANSACTION_TARGET_FILENAME
        is_ready = name == _TRANSACTION_READY_FILENAME
        is_prepared = name == _TRANSACTION_CANONICAL_FILENAME or (
            name.startswith(_TRANSACTION_PREFIX)
            and name.endswith(_TRANSACTION_SUFFIX)
            and not is_target
            and not is_ready
        )
        is_publication_temp = (
            name.startswith("..phase4-transaction")
            and ".publish-" in name
            and name.endswith(".tmp")
        )
        if name.startswith(_DETACHED_PREFIX):
            _journal_entry_identity(entry, "detached Phase 4 transaction artifact")
            detached.append(entry)
            continue
        if (
            not is_prepared
            and not is_target
            and not is_ready
            and not is_publication_temp
        ):
            continue
        _journal_entry_identity(
            entry,
            (
                "transaction journal ready companion"
                if is_ready
                else (
                    "transaction journal target companion"
                    if is_target
                    else (
                        "transaction journal"
                        if is_prepared
                        else "transaction journal publication temp"
                    )
                )
            ),
        )
        if is_ready:
            ready_paths.append(entry)
        elif is_target:
            target_paths.append(entry)
        elif is_prepared:
            prepared_paths.append(entry)
        else:
            publication_temps.append(entry)

    if detached:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "a detached Phase 4 transaction artifact requires manual recovery: "
            + ", ".join(path.name for path in detached),
        )
    if len(prepared_paths) > 1 or len(target_paths) > 1 or len(ready_paths) > 1:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "multiple pending Phase 4 transaction journal generations were found",
        )
    if len(publication_temps) > 1:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "multiple transaction journal publication temps were found",
        )

    if publication_temps:
        temporary = _load_transaction_journal(publication_temps[0], quarantine_root)
        match = _TRANSACTION_TEMP_RE.fullmatch(temporary.path.name)
        if match is None:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                f"transaction journal publication temp has an invalid name: {temporary.path}",
            )
        target = quarantine_root / match.group(1)
        if _lexists(target):
            target_journal = _load_transaction_journal(target, quarantine_root)
            target_identity = _journal_snapshot_identity(
                target_journal,
                "transaction journal target",
            )
            temporary_identity = _journal_snapshot_identity(
                temporary,
                "transaction journal publication temp",
            )
            if (
                target_identity != temporary_identity
                or target_journal.data != temporary.data
                or target_journal.file_snapshot != temporary.file_snapshot
            ):
                raise Phase4Refusal(
                    "recovery-journal-conflict",
                    "transaction journal publication temp is not its target's owned hard link",
                )
            # Older hard-link publication could leave two writable names for one
            # live journal inode.  Retaining either name as a POSIX archive would
            # preserve that alias, so automated recovery must not mutate it.
            raise Phase4Refusal(
                "recovery-journal-legacy-alias",
                "transaction journal target and publication temp are hard-link aliases; "
                "automatic cleanup is unsafe",
            )
        elif recover_publication_temp:
            _promote_publication_temp(temporary, quarantine_root)
        else:
            return temporary
        return _pending_transaction(root_value, recover_publication_temp=False)

    prepared = (
        None
        if not prepared_paths
        else _load_transaction_journal(prepared_paths[0], quarantine_root)
    )
    target_created = (
        None
        if not target_paths
        else _load_transaction_journal(target_paths[0], quarantine_root)
    )
    ready = (
        None
        if not ready_paths
        else _load_transaction_journal(ready_paths[0], quarantine_root)
    )
    if target_created is not None and prepared is not None:
        _require_target_journal_transition(prepared, target_created)
    if ready is not None:
        previous = target_created if ready.data["operation"] == "restore" else prepared
        if previous is not None:
            _require_ready_journal_transition(previous, ready)
        elif prepared is not None or target_created is not None:
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "ready transaction journal is missing its required prior generation",
            )
        return ready
    if target_created is not None:
        if prepared is None:
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "restore target companion is missing its prepared journal",
            )
        return target_created
    return prepared


def _publish_exclusive_snapshot(
    path: Path,
    base: Path,
    description: str,
    snapshot: Mapping[str, Any],
    *,
    validator: Any | None = None,
    staging_root: Path | None = None,
    staging_trusted_root: Path | None = None,
) -> ExclusiveCopy:
    """完整写入 closed-set 外的临时文件，再 no-replace 发布规范路径。"""

    if not snapshot["exists"]:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"cannot exclusively write an absent snapshot for {description}",
        )
    if _lexists(path):
        raise Phase4Refusal(
            "recovery-conflict",
            f"cannot create journal-owned path that already exists: {path}",
        )
    _ensure_directory_chain(base, path.parent, f"{description} parent")
    _validate_existing_components(base, path, description)

    publication_root = Path(staging_root) if staging_root is not None else Path(base)
    publication_trusted_root = (
        publication_root if staging_trusted_root is None else Path(staging_trusted_root)
    )
    _validate_existing_components(
        publication_trusted_root,
        Path(base),
        f"{description} publication base",
    )
    _validate_existing_components(
        publication_trusted_root,
        publication_root,
        f"{description} publication root",
    )
    publication_identity = _trusted_directory_identity(
        publication_root,
        f"{description} publication root",
    )
    if validator is None:
        temporary = publication_root / f"{_ARCHIVE_PREFIX}{uuid.uuid4().hex}.tmp"
    else:
        temporary = publication_root / (f".{path.name}.publish-{uuid.uuid4().hex}.tmp")
    staged: ExclusiveCopy | None = None
    published = False
    try:
        staged = _write_raw_exclusive_snapshot(
            temporary,
            publication_root,
            f"{description} publication staging file",
            snapshot,
        )
        if validator is not None:
            validator(temporary)
        # Move the fully durable staging inode instead of hard-linking it.  A
        # retained POSIX cleanup archive must never alias a live canonical file.
        _rename_no_replace(temporary, path)
        published = True
        if _is_link_or_junction(path) or not path.is_file():
            raise OSError(
                f"published {description} is not a trusted regular file: {path}"
            )
        if _identity_from_stat(path.lstat()) != staged.identity:
            raise OSError(
                f"published {description} identity differs from staging: {path}"
            )
        _require_snapshot_state(path, base, description, snapshot)
        _fsync_directory(path.parent)
        if publication_root != path.parent:
            _fsync_directory(publication_root)
        if (
            _trusted_directory_identity(
                publication_root,
                f"{description} publication root",
            )
            != publication_identity
        ):
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} publication root identity changed",
            )
        return ExclusiveCopy(
            path,
            staged.identity,
            publication_root,
            publication_trusted_root,
            dict(snapshot),
        )
    finally:
        if staged is not None and _lexists(temporary):
            try:
                _unlink_exclusive_copy(staged)
            except BaseException as exc:
                if published:
                    raise Phase4OperationalError(
                        "exclusive-publish-cleanup-failed",
                        f"published {description}, but cannot remove its staging link: {exc}",
                    ) from exc
                raise


def _write_transaction_journal(
    quarantine_root: Path,
    data: Mapping[str, Any],
) -> TransactionJournal:
    """完整验证后以 no-replace 方式发布持久事务日志。"""

    transaction_id = data.get("transaction_id")
    if not isinstance(transaction_id, str):
        raise Phase4Refusal(
            "recovery-journal-invalid", "journal transaction_id is missing"
        )
    path = _transaction_journal_path(quarantine_root, transaction_id)
    if _lexists(path):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"transaction journal path already exists: {path}",
        )
    snapshot = _text_snapshot(contract.stable_json_dumps(dict(data)))

    def validate_staged_journal(staged_path: Path) -> None:
        text = contract.read_utf8(staged_path)
        value = contract.parse_json_strict(text, source=str(staged_path))
        _validate_transaction_journal(value, path, quarantine_root)

    try:
        _publish_exclusive_snapshot(
            path,
            quarantine_root,
            "transaction journal",
            snapshot,
            validator=validate_staged_journal,
        )
    except FileExistsError as exc:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"transaction journal path appeared concurrently: {path}",
        ) from exc
    except (Phase4Refusal, Phase4OperationalError):
        raise
    except (OSError, contract.ProjectDocsContractError) as exc:
        raise Phase4OperationalError(
            "recovery-journal-write-failed",
            f"cannot exclusively publish transaction journal: {exc}",
        ) from exc
    return _load_transaction_journal(path, quarantine_root)


def _update_restore_target_journal(
    journal: TransactionJournal,
    target_identity: tuple[int, int, int, int],
) -> TransactionJournal:
    """Persist the independent staged target identity in an immutable companion."""

    if journal.data["phase"] != "prepared" or journal.data["operation"] != "restore":
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "only a prepared restore journal can publish a target companion",
        )
    _require_snapshot_state(
        journal.path,
        journal.quarantine_root,
        "prepared restore transaction journal",
        journal.file_snapshot,
        conflict_code="recovery-journal-conflict",
    )
    target_path = _transaction_target_path(journal.quarantine_root)
    ready_path = _transaction_ready_path(journal.quarantine_root)
    if _lexists(target_path) or _lexists(ready_path):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "a restore transaction companion already exists",
        )
    data = _journal_with_target_identity(journal, target_identity)
    snapshot = _text_snapshot(contract.stable_json_dumps(data))

    def validate_target(staged_path: Path) -> None:
        staged = _load_transaction_journal(staged_path, journal.quarantine_root)
        _require_target_journal_transition(journal, staged)

    try:
        _publish_exclusive_snapshot(
            target_path,
            journal.quarantine_root,
            "restore transaction target companion",
            snapshot,
            validator=validate_target,
        )
    except FileExistsError as exc:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"restore target companion appeared concurrently: {target_path}",
        ) from exc
    except (Phase4Refusal, Phase4OperationalError):
        raise
    except (OSError, contract.ProjectDocsContractError) as exc:
        raise Phase4OperationalError(
            "recovery-journal-write-failed",
            f"cannot exclusively publish restore target companion: {exc}",
        ) from exc
    target_created = _load_transaction_journal(
        target_path,
        journal.quarantine_root,
    )
    _require_target_journal_transition(journal, target_created)
    return target_created


def _update_transaction_journal(
    journal: TransactionJournal, data: Mapping[str, Any]
) -> TransactionJournal:
    """Publish an immutable ready companion without replacing prepared state."""

    expected_phase = (
        "prepared" if journal.data["operation"] == "quarantine" else "target-created"
    )
    if journal.data["phase"] != expected_phase:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"only a {expected_phase} journal can publish a ready companion",
        )
    _require_snapshot_state(
        journal.path,
        journal.quarantine_root,
        f"{expected_phase} transaction journal",
        journal.file_snapshot,
        conflict_code="recovery-journal-conflict",
    )
    if journal.data["operation"] == "restore":
        prepared_paths = _prepared_journal_paths(journal.quarantine_root)
        if len(prepared_paths) != 1:
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "restore target companion lacks exactly one prepared journal",
            )
        prepared = _load_transaction_journal(
            prepared_paths[0],
            journal.quarantine_root,
        )
        _require_target_journal_transition(prepared, journal)
    elif _lexists(_transaction_target_path(journal.quarantine_root)):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "quarantine transaction cannot coexist with a restore target companion",
        )
    ready_path = _transaction_ready_path(journal.quarantine_root)
    if _lexists(ready_path):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"transaction ready companion already exists: {ready_path}",
        )
    snapshot = _text_snapshot(contract.stable_json_dumps(dict(data)))

    def validate_ready(staged_path: Path) -> None:
        staged = _load_transaction_journal(staged_path, journal.quarantine_root)
        _require_ready_journal_transition(journal, staged)

    try:
        _publish_exclusive_snapshot(
            ready_path,
            journal.quarantine_root,
            "transaction journal ready companion",
            snapshot,
            validator=validate_ready,
        )
    except FileExistsError as exc:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            f"transaction ready companion appeared concurrently: {ready_path}",
        ) from exc
    except (Phase4Refusal, Phase4OperationalError):
        raise
    except (OSError, contract.ProjectDocsContractError) as exc:
        raise Phase4OperationalError(
            "recovery-journal-write-failed",
            f"cannot exclusively publish transaction ready companion: {exc}",
        ) from exc
    ready = _load_transaction_journal(ready_path, journal.quarantine_root)
    _require_ready_journal_transition(journal, ready)
    return ready


def _clear_transaction_journal(journal: TransactionJournal) -> None:
    """Remove immutable journal generations in crash-recoverable order."""

    quarantine_root = journal.quarantine_root
    docs_root = quarantine_root.parent
    current = _pending_transaction(docs_root.parent)
    if (
        current is None
        or current.path != journal.path
        or current.data != journal.data
        or current.file_snapshot != journal.file_snapshot
    ):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "the selected transaction journal is no longer the pending generation",
        )

    prepared_paths = _prepared_journal_paths(quarantine_root)
    if len(prepared_paths) > 1:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "multiple prepared transaction journals prevent cleanup",
        )
    slot_generation = (
        None
        if not prepared_paths
        else _load_transaction_journal(prepared_paths[0], quarantine_root)
    )
    prepared = (
        slot_generation
        if slot_generation is not None and slot_generation.data["phase"] == "prepared"
        else None
    )
    legacy_ready = (
        slot_generation
        if slot_generation is not None
        and slot_generation.data["phase"] == "ready"
        and _TRANSACTION_LEGACY_RE.fullmatch(slot_generation.path.name) is not None
        else None
    )
    if slot_generation is not None and prepared is None and legacy_ready is None:
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "the prepared journal slot contains an unsupported generation",
        )
    target_path = _transaction_target_path(quarantine_root)
    target_created = (
        _load_transaction_journal(target_path, quarantine_root)
        if _lexists(target_path)
        else None
    )
    ready_path = _transaction_ready_path(quarantine_root)
    ready = (
        _load_transaction_journal(ready_path, quarantine_root)
        if _lexists(ready_path)
        else legacy_ready
    )
    if legacy_ready is not None and _lexists(ready_path):
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "legacy and fixed ready transaction journals coexist",
        )

    if target_created is not None and prepared is not None:
        _require_target_journal_transition(prepared, target_created)
    if ready is not None:
        previous = target_created if ready.data["operation"] == "restore" else prepared
        if previous is not None:
            _require_ready_journal_transition(previous, ready)
        elif prepared is not None or target_created is not None:
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "ready transaction journal is missing its required prior generation",
            )

    phase = journal.data["phase"]
    operation = journal.data["operation"]
    generations: list[TransactionJournal]
    if phase == "prepared":
        if prepared is None or target_created is not None or ready is not None:
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "prepared cleanup found an unexpected companion generation",
            )
        generations = [prepared]
    elif phase == "target-created":
        if (
            operation != "restore"
            or target_created is None
            or prepared is None
            or ready is not None
        ):
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "target-created cleanup lacks its exact prepared predecessor",
            )
        # Cancellation deletes the child first, so a crash leaves a valid
        # prepared-only transaction rather than an orphan target generation.
        generations = [target_created, prepared]
    elif phase == "ready":
        if ready is None:
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "ready cleanup cannot find its canonical journal",
            )
        if operation == "restore":
            if prepared is not None and target_created is None:
                raise Phase4Refusal(
                    "recovery-journal-conflict",
                    "restore ready journal cannot skip its target companion",
                )
            generations = [
                generation
                for generation in (prepared, target_created, ready)
                if generation is not None
            ]
        else:
            if target_created is not None:
                raise Phase4Refusal(
                    "recovery-journal-conflict",
                    "quarantine ready journal cannot coexist with a target companion",
                )
            generations = [
                generation for generation in (prepared, ready) if generation is not None
            ]
    else:  # pragma: no cover - validated journal contract
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"unsupported transaction journal phase: {phase}",
        )

    owned_generations: list[tuple[TransactionJournal, tuple[int, int, int, int]]] = [
        (
            generation,
            _journal_snapshot_identity(generation, "transaction journal"),
        )
        for generation in generations
    ]

    before_artifact, after_artifact = _manifest_transaction_paths(
        docs_root,
        journal.data["transaction_id"],
    )
    artifact_specs: list[tuple[Path, Mapping[str, Any], str]] = [
        (
            before_artifact,
            journal.data["manifest"]["before"],
            "manifest transaction before anchor",
        )
    ]
    manifest_after = journal.data["manifest"]["after"]
    if manifest_after is not None:
        artifact_specs.append(
            (
                after_artifact,
                manifest_after,
                "manifest transaction after candidate",
            )
        )
    for path, snapshot, description in artifact_specs:
        if _lexists(path):
            _remove_snapshot_file(
                path,
                docs_root,
                description,
                snapshot,
                archive_root=quarantine_root,
                archive_trusted_root=docs_root,
            )

    def current_generation_paths() -> set[Path]:
        paths = set(_prepared_journal_paths(quarantine_root))
        for companion in (target_path, ready_path):
            if _lexists(companion):
                paths.add(companion)
        return paths

    for index, (generation, identity) in enumerate(owned_generations):
        pending = _pending_transaction(docs_root.parent)
        expected_paths = {item.path for item, _ in owned_generations[index:]}
        if (
            pending is None
            or pending.data["transaction_id"] != journal.data["transaction_id"]
            or current_generation_paths() != expected_paths
        ):
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "transaction journal set changed during cleanup",
            )
        _detach_and_remove_regular_file(
            generation.path,
            identity,
            "completed transaction journal",
            expected_snapshot=generation.file_snapshot,
            conflict_code="recovery-journal-conflict",
            operational_code="recovery-journal-cleanup-failed",
        )
        if generation.data["phase"] == "prepared" and len(generations) > 1:
            _transaction_fault("journal-prepared-removed")
        if generation.data["phase"] == "target-created":
            _transaction_fault("journal-target-removed")
        remaining = _pending_transaction(docs_root.parent)
        remaining_paths = {item.path for item, _ in owned_generations[index + 1 :]}
        if index + 1 == len(owned_generations):
            if remaining is not None or current_generation_paths():
                raise Phase4Refusal(
                    "recovery-journal-conflict",
                    "a transaction journal appeared after completed cleanup",
                )
        elif (
            remaining is None
            or remaining.data["transaction_id"] != journal.data["transaction_id"]
            or current_generation_paths() != remaining_paths
        ):
            raise Phase4Refusal(
                "recovery-journal-conflict",
                "transaction journal chain became incomplete during cleanup",
            )


def _transaction_fault(point: str) -> None:
    """No-op fault seam; tests raise ``BaseException`` here to mimic process loss."""

    del point


def _new_transaction_id() -> str:
    transaction_id = uuid.uuid4().hex
    if (
        _TRANSACTION_ID_RE.fullmatch(transaction_id) is None
    ):  # pragma: no cover - uuid contract
        raise Phase4OperationalError(
            "recovery-journal-id-failed", "generated transaction ID is invalid"
        )
    return transaction_id


def _journal_base(
    *,
    operation: str,
    manifest_before: str,
    claude_before: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": _TRANSACTION_SCHEMA_VERSION,
        "kind": _TRANSACTION_KIND,
        "operation": operation,
        "transaction_id": _new_transaction_id(),
        "created_at": _utc_now(),
        "phase": "prepared",
        "manifest": {"before": _text_snapshot(manifest_before), "after": None},
        "claude": {"before": dict(claude_before), "after": None},
    }


def _quarantine_journal_prepared(
    analysis: Analysis,
    moves: Sequence[QuarantineMove],
    manifest_before: str,
    claude_before: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the durable state needed to undo every not-yet-committed move."""

    journal = _journal_base(
        operation="quarantine",
        manifest_before=manifest_before,
        claude_before=claude_before,
    )
    entries: list[dict[str, Any]] = []
    for move in moves:
        source_before, source_identity = _capture_snapshot_with_identity(
            move.source_path,
            analysis.docs_root,
            f"quarantine source {move.receipt['original_path']}",
        )
        if source_identity is None:
            raise Phase4Refusal(
                "candidate-path-changed",
                f"candidate disappeared before journal creation: "
                f"{move.receipt['original_path']}",
            )
        move.receipt["payload_identity"] = list(source_identity)
        if source_before["sha256"] != move.receipt["sha256"]:
            raise Phase4Refusal(
                "candidate-hash-changed",
                f"candidate changed before journal creation: {move.receipt['original_path']}",
            )
        entries.append(
            {
                "receipt_id": move.receipt["receipt_id"],
                "original_path": move.receipt["original_path"],
                "source_before": source_before,
                "receipt_before": _absent_snapshot(),
                "receipt_after": _text_snapshot(
                    contract.stable_json_dumps(move.receipt)
                ),
                "staging_name": move.staging_dir.name,
            }
        )
    journal["quarantine"] = {"moves": entries}
    return journal


def _restore_journal_prepared(
    *,
    manifest_before: str,
    claude_before: Mapping[str, Any],
    record: ReceiptRecord,
    receipt_before: Mapping[str, Any],
    payload_before: Mapping[str, Any],
    updated_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Build restore intent before creating any staged or active target inode."""

    journal = _journal_base(
        operation="restore",
        manifest_before=manifest_before,
        claude_before=claude_before,
    )
    target_after = _text_snapshot(payload_before["text"])
    journal["restore"] = {
        "receipt_id": record.data["receipt_id"],
        "original_path": record.data["original_path"],
        "target_before": _absent_snapshot(),
        "target_after": target_after,
        "target_identity": None,
        "receipt_before": dict(receipt_before),
        "receipt_after": _text_snapshot(
            contract.stable_json_dumps(dict(updated_receipt))
        ),
        "payload_before": dict(payload_before),
        "payload_after": _absent_snapshot(),
    }
    return journal


def _journal_with_target_identity(
    journal: TransactionJournal,
    target_identity: tuple[int, int, int, int],
) -> dict[str, Any]:
    """Prepare the immutable target-created generation for a restore."""

    if journal.data["phase"] != "prepared" or journal.data["operation"] != "restore":
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore target identity can only extend a prepared restore journal",
        )
    payload_identity = _restore_journal_payload_identity(journal.data["restore"])
    if target_identity == payload_identity:
        raise Phase4Refusal(
            "restore-conflict",
            "restore staged target aliases the quarantine payload identity",
        )
    updated = dict(journal.data)
    updated["phase"] = "target-created"
    restore = dict(journal.data["restore"])
    restore["target_identity"] = list(target_identity)
    updated["restore"] = restore
    return updated


def _journal_with_metadata(
    journal: TransactionJournal,
    *,
    manifest_after: Mapping[str, Any],
    claude_after: Mapping[str, Any],
) -> dict[str, Any]:
    """Prepare a ready journal before any CLAUDE, receipt, or manifest publish."""

    expected_phase = (
        "prepared" if journal.data["operation"] == "quarantine" else "target-created"
    )
    if journal.data["phase"] != expected_phase:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "transaction journal is not ready for metadata publication",
        )
    updated = dict(journal.data)
    updated["phase"] = "ready"
    updated["manifest"] = {
        "before": dict(journal.data["manifest"]["before"]),
        "after": _text_snapshot(contract.stable_json_dumps(dict(manifest_after))),
    }
    updated["claude"] = {
        "before": dict(journal.data["claude"]["before"]),
        "after": dict(claude_after),
    }
    return updated


def _journal_document_path(
    docs_root: Path, original_path: str, description: str
) -> Path:
    try:
        safe = contract.resolve_manifest_document(
            docs_root, original_path, must_exist=False
        )
        _validate_existing_components(docs_root, safe.resolved_path, description)
        return safe.resolved_path
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("recovery-journal-invalid", str(exc)) from exc


def _journal_receipt_paths(
    quarantine_root: Path,
    receipt_id: str,
    original_path: str,
    staging_name: str | None = None,
) -> tuple[Path, Path, Path, Path, Path | None, Path | None]:
    """Derive every receipt path from validated IDs instead of trusting JSON paths."""

    _validate_receipt_id(receipt_id)
    pure_original = contract.validate_manifest_relative_path(original_path)
    final_dir = quarantine_root / receipt_id
    final_receipt = final_dir / RECEIPT_FILENAME
    final_payload = final_dir / PAYLOAD_DIRNAME
    final_payload = final_payload.joinpath(*pure_original.parts)
    if staging_name is None:
        return final_dir, final_receipt, final_payload, final_dir, None, None
    staging = quarantine_root / _validate_staging_name(staging_name)
    staging_receipt = staging / RECEIPT_FILENAME
    staging_payload = staging / PAYLOAD_DIRNAME
    staging_payload = staging_payload.joinpath(*pure_original.parts)
    return (
        final_dir,
        final_receipt,
        final_payload,
        staging,
        staging_receipt,
        staging_payload,
    )


def _ensure_directory_chain(
    base: Path,
    directory: Path,
    description: str,
) -> None:
    """Create real descendant directories and durably publish every new entry."""

    base = Path(base)
    directory = Path(directory)
    _require_below(directory, base, description)
    base_identity = _trusted_directory_identity(base, f"{description} base")
    current = base
    for part in directory.relative_to(base).parts:
        child = current / part
        if _lexists(child):
            if _is_link_or_junction(child) or not child.is_dir():
                raise Phase4Refusal(
                    "untrusted-path",
                    f"{description} contains a non-directory or indirect component: {child}",
                )
        else:
            try:
                child.mkdir()
            except FileExistsError:
                if _is_link_or_junction(child) or not child.is_dir():
                    raise Phase4Refusal(
                        "untrusted-path",
                        f"{description} appeared as an untrusted component: {child}",
                    )
            except OSError as exc:
                raise Phase4OperationalError(
                    "recovery-write-failed",
                    f"cannot create {description} component {child}: {exc}",
                ) from exc
            else:
                _fsync_directory(current)
                _fsync_directory(child)
        current = child
    if _trusted_directory_identity(base, f"{description} base") != base_identity:
        raise Phase4Refusal(
            "untrusted-path",
            f"{description} base identity changed while creating parents: {base}",
        )


def _write_raw_exclusive_snapshot(
    path: Path,
    base: Path,
    description: str,
    snapshot: Mapping[str, Any],
) -> ExclusiveCopy:
    """Write one private staging file; callers must not expose it as canonical."""

    if not snapshot["exists"]:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"cannot exclusively write an absent snapshot for {description}",
        )
    if _lexists(path):
        raise Phase4Refusal(
            "recovery-conflict",
            f"cannot create journal-owned path that already exists: {path}",
        )
    _ensure_directory_chain(base, path.parent, f"{description} parent")
    _validate_existing_components(base, path, description)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = -1
    identity: tuple[int, int, int, int] | None = None
    try:
        fd = os.open(path, flags, 0o600)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(f"journal destination is not regular: {path}")
        identity = _identity_from_stat(opened)
        payload = snapshot["text"].encode("utf-8")
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                raise OSError(f"short journal destination write: {path}")
            written += count
            if written == count:
                _transaction_fault("exclusive-staging-write-started")
        os.fsync(fd)
        os.close(fd)
        fd = -1
        if _is_link_or_junction(path) or _identity_from_stat(path.lstat()) != identity:
            raise OSError(f"journal destination identity changed: {path}")
        _require_snapshot_state(path, base, description, snapshot)
        _fsync_directory(path.parent)
        return ExclusiveCopy(path, identity, base, base, dict(snapshot))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        if identity is not None and _lexists(path):
            try:
                _unlink_exclusive_copy(
                    ExclusiveCopy(path, identity, base, base, dict(snapshot))
                )
            except BaseException:
                pass
        raise


def _write_exclusive_snapshot(
    path: Path,
    base: Path,
    description: str,
    snapshot: Mapping[str, Any],
    *,
    staging_root: Path | None = None,
    staging_trusted_root: Path | None = None,
) -> ExclusiveCopy:
    """Atomically publish one missing canonical file after durable staging."""

    return _publish_exclusive_snapshot(
        path,
        base,
        description,
        snapshot,
        staging_root=staging_root,
        staging_trusted_root=staging_trusted_root,
    )


def _raise_rename_errno(error: int, destination: Path) -> None:
    """Translate one rename errno into the publisher's exception taxonomy."""

    message = os.strerror(error)
    if error in _NO_REPLACE_UNSUPPORTED_ERRNOS:
        raise NoReplaceRenameUnsupported(
            errno.ENOTSUP,
            "atomic no-replace rename is not supported by this kernel or "
            f"filesystem ({message}); refusing to fall back to a replacing rename",
            os.fspath(destination),
        )
    raise OSError(error, message, os.fspath(destination))


def _linux_rename_no_replace(source: Path, destination: Path) -> None:
    """Linux ``RENAME_NOREPLACE``; fails closed when the kernel lacks it."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:  # pragma: no cover - unusual libc
        raise NoReplaceRenameUnsupported(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable: libc does not export renameat2",
            os.fspath(destination),
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        != 0
    ):
        _raise_rename_errno(ctypes.get_errno(), destination)


def _rename_no_replace(source: Path, destination: Path) -> None:
    """Atomically rename without replacing a concurrently created destination."""

    source = Path(source)
    destination = Path(destination)
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(source, destination)
        return
    if sys.platform == "darwin":  # pragma: no cover - exercised on macOS only
        try:
            renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "renamex_np is unavailable") from exc
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        if renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), os.fspath(destination))
        return
    raise OSError(
        errno.ENOTSUP,
        f"atomic no-replace rename is unsupported on {sys.platform}",
        os.fspath(destination),
    )


def _delete_regular_file_by_handle_windows(
    path: Path,
    expected_identity: tuple[int, int, int, int],
    expected_snapshot: Mapping[str, Any],
) -> None:
    """Verify and delete the exact NTFS file through one no-share-write handle."""

    import msvcrt

    generic_read = 0x80000000
    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    share_read_delete = 0x00000001 | 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = ctypes.c_void_p(-1).value
    file_disposition_info_ex = 21
    file_disposition_delete = 0x00000001
    file_disposition_posix_semantics = 0x00000002
    file_disposition_ignore_readonly_attribute = 0x00000010

    class FileDispositionInfoEx(ctypes.Structure):
        _fields_ = [("flags", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_file_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = create_file(
        os.fspath(path),
        generic_read | delete_access | file_read_attributes,
        share_read_delete,
        None,
        open_existing,
        file_flag_open_reparse_point,
        None,
    )
    if handle in (None, invalid_handle_value):
        raise ctypes.WinError(ctypes.get_last_error())
    fd = -1
    try:
        try:
            fd = msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            close_handle(handle)
            raise
        raw, opened = _read_stable_regular_fd(fd, path)
        if _identity_from_stat(opened) != expected_identity:
            raise OSError(
                f"detached file identity changed before handle deletion: {path}"
            )
        try:
            snapshot = _text_snapshot(raw.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise OSError(
                f"detached file is no longer strict UTF-8: {path}: {exc}"
            ) from exc
        if snapshot != dict(expected_snapshot):
            raise OSError(
                f"detached file content changed before handle deletion: {path}"
            )
        disposition = FileDispositionInfoEx(
            file_disposition_delete
            | file_disposition_posix_semantics
            | file_disposition_ignore_readonly_attribute
        )
        raw_handle = msvcrt.get_osfhandle(fd)
        if not set_file_information(
            raw_handle,
            file_disposition_info_ex,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if fd >= 0:
            os.close(fd)


def _delete_empty_directory_by_handle_windows(
    path: Path,
    expected_identity: tuple[int, int, int, int],
) -> None:
    """Delete one opened empty NT directory, never a later path replacement."""

    import msvcrt

    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    invalid_handle_value = ctypes.c_void_p(-1).value
    file_disposition_info_ex = 21
    file_disposition_delete = 0x00000001
    file_disposition_posix_semantics = 0x00000002
    file_disposition_ignore_readonly_attribute = 0x00000010

    class FileDispositionInfoEx(ctypes.Structure):
        _fields_ = [("flags", ctypes.c_uint32)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_file_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    handle = create_file(
        os.fspath(path),
        delete_access | file_read_attributes,
        share_all,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle in (None, invalid_handle_value):
        raise ctypes.WinError(ctypes.get_last_error())
    fd = -1
    try:
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            close_handle(handle)
            raise
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _identity_from_stat(opened) != expected_identity
        ):
            raise OSError(
                f"detached directory identity changed before handle deletion: {path}"
            )
        disposition = FileDispositionInfoEx(
            file_disposition_delete
            | file_disposition_posix_semantics
            | file_disposition_ignore_readonly_attribute
        )
        raw_handle = msvcrt.get_osfhandle(fd)
        if not set_file_information(
            raw_handle,
            file_disposition_info_ex,
            ctypes.byref(disposition),
            ctypes.sizeof(disposition),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if fd >= 0:
            os.close(fd)


def _detach_and_remove_regular_file(
    path: Path,
    expected_identity: tuple[int, int, int, int],
    description: str,
    *,
    expected_snapshot: Mapping[str, Any] | None = None,
    conflict_code: str = "recovery-conflict",
    operational_code: str = "recovery-cleanup-failed",
    archive_root: Path | None = None,
    archive_trusted_root: Path | None = None,
    is_windows: bool | None = None,
) -> None:
    """Detach and dispose of only the exact file owned by this transaction."""

    windows = os.name == "nt" if is_windows is None else is_windows
    parent = path.parent
    parent_identity = _trusted_directory_identity(parent, f"{description} parent")
    archive_parent = parent
    trusted_root = parent
    if not windows and archive_root is not None:
        archive_parent = Path(archive_root)
        trusted_root = (
            archive_parent
            if archive_trusted_root is None
            else Path(archive_trusted_root)
        )
        _validate_existing_components(
            trusted_root,
            parent,
            f"{description} source parent",
        )
        _validate_existing_components(
            trusted_root,
            archive_parent,
            f"{description} archive root",
        )
    archive_parent_identity = _trusted_directory_identity(
        archive_parent,
        f"{description} archive root",
    )
    if _is_link_or_junction(path):
        raise Phase4Refusal(conflict_code, f"{description} became indirect: {path}")
    try:
        current = path.lstat()
    except OSError as exc:
        raise Phase4Refusal(
            conflict_code,
            f"cannot inspect {description} before detaching it: {path}: {exc}",
        ) from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or _identity_from_stat(current) != expected_identity
    ):
        raise Phase4Refusal(
            conflict_code,
            f"{description} identity changed before detachment: {path}",
        )

    tombstone_prefix = _DETACHED_PREFIX if windows else _ARCHIVE_PREFIX
    tombstone = archive_parent / f"{tombstone_prefix}{uuid.uuid4().hex}.tmp"
    if _lexists(tombstone):  # pragma: no cover - UUID collision or hostile writer
        raise Phase4Refusal(
            conflict_code,
            f"detachment tombstone unexpectedly exists: {tombstone}",
        )
    try:
        _rename_no_replace(path, tombstone)
    except OSError as exc:
        raise Phase4OperationalError(
            operational_code,
            f"cannot detach {description} before removal: {exc}",
        ) from exc

    mismatch: str | None = None
    moved_identity: tuple[int, int, int, int] | None = None
    try:
        moved_snapshot, moved_identity = _capture_snapshot_with_identity(
            tombstone,
            trusted_root,
            f"detached {description}",
        )
        if moved_identity != expected_identity:
            mismatch = "identity"
        elif expected_snapshot is not None and moved_snapshot != dict(
            expected_snapshot
        ):
            mismatch = "content"
    except Phase4Refusal as exc:
        mismatch = f"trust ({exc.message})"

    if mismatch is not None:
        restore_detail = "detached object retained at its tombstone"
        if not _lexists(path):
            try:
                _rename_no_replace(tombstone, path)
                _, restored_identity = _capture_snapshot_with_identity(
                    path,
                    parent,
                    f"restored {description}",
                )
                if restored_identity != moved_identity:
                    raise OSError(
                        "restored entry identity differs from detached object"
                    )
                restore_detail = "detached object restored to its original path"
            except (OSError, Phase4Refusal) as exc:
                restore_detail = (
                    f"detached object retained because restoration failed: {exc}"
                )
        else:
            restore_detail = (
                "detached object retained because its original path was recreated"
            )
        _fsync_directory(parent)
        if archive_parent != parent:
            _fsync_directory(archive_parent)
        raise Phase4Refusal(
            conflict_code,
            f"{description} changed during detachment ({mismatch}); "
            f"{restore_detail}: {path}",
        )

    if _lexists(path):
        _fsync_directory(parent)
        if archive_parent != parent:
            _fsync_directory(archive_parent)
        raise Phase4Refusal(
            conflict_code,
            f"{description} path was recreated after detachment; the owned object "
            f"was retained at {tombstone}",
        )
    if _trusted_directory_identity(parent, f"{description} parent") != parent_identity:
        raise Phase4Refusal(
            conflict_code,
            f"{description} parent identity changed during detachment: {parent}",
        )
    if (
        _trusted_directory_identity(
            archive_parent,
            f"{description} archive root",
        )
        != archive_parent_identity
    ):
        raise Phase4Refusal(
            conflict_code,
            f"{description} archive root identity changed: {archive_parent}",
        )
    try:
        if windows:
            if expected_snapshot is None:
                raise OSError(
                    f"exact snapshot is required for Windows handle deletion: {tombstone}"
                )
            _delete_regular_file_by_handle_windows(
                tombstone,
                expected_identity,
                expected_snapshot,
            )
            if _lexists(tombstone):
                remaining = tombstone.lstat()
                if _identity_from_stat(remaining) == expected_identity:
                    raise OSError(
                        f"detached file remained after handle deletion: {tombstone}"
                    )
                raise Phase4Refusal(
                    conflict_code,
                    f"a foreign file replaced the deleted tombstone: {tombstone}",
                )
        # POSIX has no portable identity-bound unlink.  Retain the unique archive
        # at the trusted root, outside receipt/staging closed sets.
        _fsync_directory(parent)
        if archive_parent != parent:
            _fsync_directory(archive_parent)
    except Phase4Refusal:
        raise
    except OSError as exc:
        raise Phase4OperationalError(
            operational_code,
            f"cannot remove detached {description}: {exc}",
        ) from exc


def _remove_snapshot_file(
    path: Path,
    base: Path,
    description: str,
    expected: Mapping[str, Any],
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
    archive_root: Path | None = None,
    archive_trusted_root: Path | None = None,
) -> None:
    """Delete one exact snapshot through an identity-bound tombstone."""

    current, identity = _capture_snapshot_with_identity(path, base, description)
    if current != dict(expected):
        raise Phase4Refusal(
            "recovery-conflict",
            f"{description} differs from the transaction journal: {path}",
        )
    if not expected["exists"]:
        return
    if identity is None or (
        expected_identity is not None and identity != expected_identity
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            f"{description} identity changed before removal: {path}",
        )
    _detach_and_remove_regular_file(
        path,
        identity,
        description,
        expected_snapshot=expected,
        archive_root=base if archive_root is None else archive_root,
        archive_trusted_root=archive_trusted_root,
    )
    if _lexists(path):
        raise Phase4OperationalError(
            "recovery-cleanup-failed",
            f"{description} remained after removal: {path}",
        )


def _metadata_snapshot_artifact_path(
    publication_root: Path,
    publication_trusted_root: Path,
    path: Path,
    snapshot: Mapping[str, Any],
) -> Path:
    """Derive a stable transaction artifact slot without exposing absolute paths."""

    try:
        relative = path.relative_to(publication_trusted_root).as_posix()
    except ValueError as exc:
        raise Phase4Refusal(
            "recovery-conflict",
            f"metadata path is outside its trusted publication root: {path}",
        ) from exc
    key = contract.stable_json_dumps(
        {
            "path": relative,
            "snapshot": dict(snapshot),
        }
    )
    digest = contract.sha256_bytes(key.encode("utf-8"))
    return publication_root / f"{_ARCHIVE_PREFIX}metadata-{digest}.tmp"


def _retire_metadata_snapshot_artifact(
    artifact: Path,
    publication_trusted_root: Path,
    publication_root: Path,
    description: str,
    snapshot: Mapping[str, Any],
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> None:
    if not _lexists(artifact):
        return
    _remove_snapshot_file(
        artifact,
        publication_trusted_root,
        description,
        snapshot,
        expected_identity=expected_identity,
        archive_root=publication_root,
        archive_trusted_root=publication_trusted_root,
    )


def _set_snapshot_state_impl(
    path: Path,
    base: Path,
    description: str,
    desired: Mapping[str, Any],
    allowed_current: Sequence[Mapping[str, Any]],
    *,
    archive_root: Path | None = None,
    archive_trusted_root: Path | None = None,
) -> None:
    """Move metadata through durable pre/postimage artifacts and no-replace renames."""

    publication_root = Path(base) if archive_root is None else Path(archive_root)
    publication_trusted_root = (
        publication_root if archive_trusted_root is None else Path(archive_trusted_root)
    )
    _validate_existing_components(
        publication_trusted_root,
        publication_root,
        f"{description} publication root",
    )
    normalized_allowed = [dict(snapshot) for snapshot in allowed_current]
    desired_snapshot = dict(desired)
    known_snapshots: list[dict[str, Any]] = []
    for snapshot in [*normalized_allowed, desired_snapshot]:
        if snapshot not in known_snapshots:
            known_snapshots.append(snapshot)
    artifacts = {
        contract.stable_json_dumps(snapshot): _metadata_snapshot_artifact_path(
            publication_root,
            publication_trusted_root,
            path,
            snapshot,
        )
        for snapshot in known_snapshots
        if snapshot["exists"]
    }

    current, current_identity = _capture_snapshot_with_identity(
        path,
        base,
        description,
    )
    if current == desired_snapshot:
        for snapshot in known_snapshots:
            if not snapshot["exists"]:
                continue
            artifact = artifacts[contract.stable_json_dumps(snapshot)]
            _retire_metadata_snapshot_artifact(
                artifact,
                publication_trusted_root,
                publication_root,
                f"{description} completed metadata artifact",
                snapshot,
            )
        return
    if current == _absent_snapshot() and desired_snapshot["exists"]:
        desired_artifact = artifacts[contract.stable_json_dumps(desired_snapshot)]
        counterpart_snapshots = [
            snapshot
            for snapshot in normalized_allowed
            if snapshot["exists"] and snapshot != desired_snapshot
        ]
        existing_counterparts = [
            snapshot
            for snapshot in counterpart_snapshots
            if _lexists(artifacts[contract.stable_json_dumps(snapshot)])
        ]
        if not _lexists(desired_artifact) or not existing_counterparts:
            if _absent_snapshot() in normalized_allowed:
                _write_exclusive_snapshot(
                    path,
                    base,
                    description,
                    desired_snapshot,
                    staging_root=publication_root,
                    staging_trusted_root=publication_trusted_root,
                )
                _require_snapshot_state(path, base, description, desired_snapshot)
                _fsync_directory(path.parent)
                return
            raise Phase4Refusal(
                "recovery-conflict",
                f"absent {description} is not a complete transaction-owned metadata gap",
            )
        desired_current, desired_identity = _capture_snapshot_with_identity(
            desired_artifact,
            publication_trusted_root,
            f"{description} desired metadata artifact",
        )
        if desired_current != desired_snapshot or desired_identity is None:
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} desired metadata artifact changed",
            )
        for snapshot in existing_counterparts:
            _require_snapshot_state(
                artifacts[contract.stable_json_dumps(snapshot)],
                publication_trusted_root,
                f"{description} preimage metadata artifact",
                snapshot,
                conflict_code="recovery-conflict",
            )
        try:
            _rename_no_replace(desired_artifact, path)
        except FileExistsError as exc:
            raise Phase4Refusal(
                "recovery-conflict",
                f"a concurrent {description} appeared in its metadata gap",
            ) from exc
        _require_snapshot_state(path, base, description, desired_snapshot)
        if _identity_from_stat(path.lstat()) != desired_identity:
            raise Phase4Refusal(
                "recovery-conflict",
                f"published {description} identity differs from its metadata artifact",
            )
        _fsync_directory(path.parent)
        if publication_root != path.parent:
            _fsync_directory(publication_root)
        for snapshot in existing_counterparts:
            _retire_metadata_snapshot_artifact(
                artifacts[contract.stable_json_dumps(snapshot)],
                publication_trusted_root,
                publication_root,
                f"{description} recovered preimage metadata artifact",
                snapshot,
            )
        return
    if not any(current == snapshot for snapshot in normalized_allowed):
        raise Phase4Refusal(
            "recovery-conflict",
            f"{description} differs from every permitted journal state: {path}",
        )
    if not desired_snapshot["exists"]:
        if not current["exists"]:
            return
        _remove_snapshot_file(
            path,
            base,
            description,
            current,
            expected_identity=current_identity,
            archive_root=publication_root,
            archive_trusted_root=publication_trusted_root,
        )
        return
    if not current["exists"] or current_identity is None:
        _write_exclusive_snapshot(
            path,
            base,
            description,
            desired_snapshot,
            staging_root=publication_root,
            staging_trusted_root=publication_trusted_root,
        )
        _require_snapshot_state(path, base, description, desired_snapshot)
        _fsync_directory(path.parent)
        return

    current_artifact = artifacts[contract.stable_json_dumps(current)]
    desired_artifact = artifacts[contract.stable_json_dumps(desired_snapshot)]
    for snapshot, artifact in (
        (current, current_artifact),
        (desired_snapshot, desired_artifact),
    ):
        _retire_metadata_snapshot_artifact(
            artifact,
            publication_trusted_root,
            publication_root,
            f"{description} stale metadata artifact",
            snapshot,
        )
    staged = _write_exclusive_snapshot(
        desired_artifact,
        publication_trusted_root,
        f"{description} desired metadata artifact",
        desired_snapshot,
        staging_root=publication_root,
        staging_trusted_root=publication_trusted_root,
    )
    detached = False
    published = False
    try:
        current_again, identity_again = _capture_snapshot_with_identity(
            path,
            base,
            description,
        )
        if current_again != current or identity_again != current_identity:
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} changed before metadata detachment",
            )
        try:
            _rename_no_replace(path, current_artifact)
        except FileExistsError as exc:
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} preimage artifact appeared concurrently",
            ) from exc
        detached = True
        moved_current, moved_identity = _capture_snapshot_with_identity(
            current_artifact,
            publication_trusted_root,
            f"{description} preimage metadata artifact",
        )
        if moved_current != current or moved_identity != current_identity:
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} preimage changed during metadata detachment",
            )
        _fsync_directory(path.parent)
        if publication_root != path.parent:
            _fsync_directory(publication_root)
        _transaction_fault("metadata-preimage-detached")
        try:
            _rename_no_replace(desired_artifact, path)
        except FileExistsError as exc:
            raise Phase4Refusal(
                "recovery-conflict",
                f"a concurrent {description} appeared in its metadata gap",
            ) from exc
        published = True
        if _identity_from_stat(path.lstat()) != staged.identity:
            raise Phase4Refusal(
                "recovery-conflict",
                f"published {description} identity differs from its staged postimage",
            )
        _require_snapshot_state(path, base, description, desired_snapshot)
        _fsync_directory(path.parent)
        if publication_root != path.parent:
            _fsync_directory(publication_root)
    except Exception as publish_exc:
        rollback_exc: BaseException | None = None
        if detached and not published and not _lexists(path):
            try:
                _rename_no_replace(current_artifact, path)
                _require_snapshot_state(path, base, description, current)
                _fsync_directory(path.parent)
                if publication_root != path.parent:
                    _fsync_directory(publication_root)
            except BaseException as exc:
                rollback_exc = exc
        if not published and _lexists(desired_artifact):
            try:
                _retire_metadata_snapshot_artifact(
                    desired_artifact,
                    publication_trusted_root,
                    publication_root,
                    f"{description} abandoned postimage metadata artifact",
                    desired_snapshot,
                    expected_identity=staged.identity,
                )
            except BaseException as exc:
                if rollback_exc is None:
                    rollback_exc = exc
        if rollback_exc is not None:
            raise Phase4OperationalError(
                "recovery-write-failed",
                f"cannot publish {description} ({publish_exc}); "
                f"cannot restore a complete metadata state ({rollback_exc})",
            ) from publish_exc
        raise
    _retire_metadata_snapshot_artifact(
        current_artifact,
        publication_trusted_root,
        publication_root,
        f"{description} committed preimage metadata artifact",
        current,
        expected_identity=current_identity,
    )


def _set_snapshot_state(
    path: Path,
    base: Path,
    description: str,
    desired: Mapping[str, Any],
    allowed_current: Sequence[Mapping[str, Any]],
    *,
    archive_root: Path | None = None,
    archive_trusted_root: Path | None = None,
    archive_root_expected_identity: tuple[int, int, int, int] | None = None,
) -> None:
    """Publish metadata while binding an optional transaction artifact root."""

    publication_root = Path(base) if archive_root is None else Path(archive_root)
    publication_trusted_root = (
        publication_root if archive_trusted_root is None else Path(archive_trusted_root)
    )

    def require_expected_root() -> None:
        if archive_root_expected_identity is None:
            return
        try:
            _validate_existing_components(
                publication_trusted_root,
                publication_root,
                f"{description} publication root",
            )
            observed = _trusted_directory_identity(
                publication_root,
                f"{description} publication root",
            )
        except Phase4Refusal as exc:
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} publication root is no longer trusted: {exc.message}",
            ) from exc
        if observed != archive_root_expected_identity:
            raise Phase4Refusal(
                "recovery-conflict",
                f"{description} publication root identity changed",
            )

    require_expected_root()
    try:
        _set_snapshot_state_impl(
            path,
            base,
            description,
            desired,
            allowed_current,
            archive_root=archive_root,
            archive_trusted_root=archive_trusted_root,
        )
    except BaseException as exc:
        try:
            require_expected_root()
        except Phase4Refusal as identity_exc:
            raise identity_exc from exc
        raise
    require_expected_root()


def _closed_directory_inventory(
    directory: Path,
) -> tuple[set[str], set[str]]:
    """Enumerate one transaction tree independently of the primary ``os.walk``."""

    directories = {"."}
    files: set[str] = set()
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError as exc:
            raise Phase4Refusal(
                "recovery-conflict",
                f"cannot enumerate transaction directory {current}: {exc}",
            ) from exc
        for entry in entries:
            child = Path(entry.path)
            relative = child.relative_to(directory).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"cannot inspect transaction member {child}: {exc}",
                ) from exc
            if entry.is_symlink() or bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction directory contains an indirect member: {child}",
                )
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                pending.append(child)
            elif stat.S_ISREG(info.st_mode):
                files.add(relative)
            else:
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction directory contains a non-regular member: {child}",
                )
    return directories, files


def _require_closed_transaction_directory(
    directory: Path,
    quarantine_root: Path,
    expected_files: Mapping[str, Mapping[str, Any]],
    *,
    directory_identities: dict[str, tuple[int, int, int, int]] | None = None,
    file_identities: dict[str, tuple[int, int, int, int]] | None = None,
) -> tuple[int, int, int, int]:
    """验证事务目录只含精确声明的目录和文件，并返回根目录身份。"""

    _validate_existing_components(quarantine_root, directory, "transaction directory")
    expected_identity = _trusted_directory_identity(directory, "transaction directory")
    expected = {
        PurePosixPath(key).as_posix(): dict(value)
        for key, value in expected_files.items()
    }
    prefixes: set[str] = {"."}
    for relative in expected:
        pure = PurePosixPath(relative)
        for parent in pure.parents:
            prefixes.add(parent.as_posix())
    found_files: dict[str, Path] = {}
    found_directory_identities: dict[str, tuple[int, int, int, int]] = {}
    walk_errors: list[OSError] = []

    def onerror(error: OSError) -> None:
        walk_errors.append(error)

    for current_text, dirnames, filenames in os.walk(
        directory,
        topdown=True,
        followlinks=False,
        onerror=onerror,
    ):
        current = Path(current_text)
        relative_dir = (
            current.relative_to(directory).as_posix() if current != directory else "."
        )
        if relative_dir not in prefixes or _is_link_or_junction(current):
            raise Phase4Refusal(
                "recovery-conflict",
                f"transaction directory contains an unowned directory: {current}",
            )
        found_directory_identities[relative_dir] = _trusted_directory_identity(
            current,
            f"transaction directory {relative_dir}",
        )
        for dirname in list(dirnames):
            child = current / dirname
            relative_child = child.relative_to(directory).as_posix()
            if relative_child not in prefixes or _is_link_or_junction(child):
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction directory contains an unowned or indirect child: {child}",
                )
        for filename in filenames:
            child = current / filename
            relative = child.relative_to(directory).as_posix()
            if (
                relative not in expected
                or _is_link_or_junction(child)
                or not child.is_file()
            ):
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction directory contains an unowned file: {child}",
                )
            found_files[relative] = child
    if walk_errors:
        raise Phase4Refusal(
            "recovery-conflict",
            f"cannot completely enumerate transaction directory: {walk_errors[0]}",
        )
    if (
        _trusted_directory_identity(directory, "transaction directory")
        != expected_identity
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            f"transaction directory identity changed during validation: {directory}",
        )
    found_file_identities: dict[str, tuple[int, int, int, int]] = {}
    for relative, snapshot in expected.items():
        path = found_files.get(relative)
        if snapshot["exists"]:
            if path is None:
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction directory lost expected file: {directory / relative}",
                )
            observed, identity = _capture_snapshot_with_identity(
                path,
                directory,
                f"transaction file {relative}",
            )
            if observed != snapshot or identity is None:
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction file differs from its expected snapshot: {path}",
                )
            found_file_identities[relative] = identity
        elif path is not None:
            raise Phase4Refusal(
                "recovery-conflict",
                f"transaction directory unexpectedly contains: {path}",
            )
    for relative, identity in found_directory_identities.items():
        path = (
            directory
            if relative == "."
            else directory.joinpath(*PurePosixPath(relative).parts)
        )
        if (
            _trusted_directory_identity(path, f"transaction directory {relative}")
            != identity
        ):
            raise Phase4Refusal(
                "recovery-conflict",
                f"transaction directory identity changed during validation: {path}",
            )
    final_directories, final_files = _closed_directory_inventory(directory)
    expected_directories = set(found_directory_identities)
    expected_present_files = {
        relative for relative, snapshot in expected.items() if snapshot["exists"]
    }
    if (
        final_directories != expected_directories
        or final_files != expected_present_files
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            "transaction directory membership changed during validation "
            f"(directories={sorted(final_directories)}, files={sorted(final_files)})",
        )
    for relative, expected_file_identity in found_file_identities.items():
        path = directory.joinpath(*PurePosixPath(relative).parts)
        observed, observed_identity = _capture_snapshot_with_identity(
            path,
            directory,
            f"transaction file {relative}",
        )
        if (
            observed != expected[relative]
            or observed_identity != expected_file_identity
        ):
            raise Phase4Refusal(
                "recovery-conflict",
                f"transaction file changed during validation: {path}",
            )
    if directory_identities is not None:
        directory_identities.clear()
        directory_identities.update(found_directory_identities)
    if file_identities is not None:
        file_identities.clear()
        file_identities.update(found_file_identities)
    return expected_identity


def _safe_remove_transaction_directory(
    directory: Path,
    quarantine_root: Path,
    expected_files: Mapping[str, Mapping[str, Any]],
    *,
    expected_file_identities: Mapping[str, tuple[int, int, int, int]] | None = None,
) -> None:
    """Detach a closed owned directory and dispose of it without path-racy deletes."""

    if not _lexists(directory):
        return
    expected = {
        PurePosixPath(key).as_posix(): dict(value)
        for key, value in expected_files.items()
    }
    original_directories: dict[str, tuple[int, int, int, int]] = {}
    original_files: dict[str, tuple[int, int, int, int]] = {}
    expected_identity = _require_closed_transaction_directory(
        directory,
        quarantine_root,
        expected,
        directory_identities=original_directories,
        file_identities=original_files,
    )
    if expected_file_identities is not None:
        normalized_expected_identities = {
            PurePosixPath(key).as_posix(): value
            for key, value in expected_file_identities.items()
        }
        for relative, identity in normalized_expected_identities.items():
            if original_files.get(relative) != identity:
                raise Phase4Refusal(
                    "recovery-conflict",
                    f"transaction file identity differs from persisted ownership: {relative}",
                )
    parent = directory.parent
    parent_identity = _trusted_directory_identity(
        parent, "transaction directory parent"
    )
    tombstone_prefix = _DETACHED_PREFIX if os.name == "nt" else _ARCHIVE_PREFIX
    tombstone = parent / f"{tombstone_prefix}{uuid.uuid4().hex}.dir"
    if _lexists(tombstone):  # pragma: no cover - UUID collision or hostile writer
        raise Phase4Refusal(
            "recovery-conflict",
            f"transaction directory tombstone unexpectedly exists: {tombstone}",
        )
    try:
        _rename_no_replace(directory, tombstone)
    except OSError as exc:
        raise Phase4OperationalError(
            "recovery-cleanup-failed",
            f"cannot detach transaction directory {directory}: {exc}",
        ) from exc

    moved_directories: dict[str, tuple[int, int, int, int]] = {}
    moved_files: dict[str, tuple[int, int, int, int]] = {}
    moved_identity = _require_closed_transaction_directory(
        tombstone,
        quarantine_root,
        expected,
        directory_identities=moved_directories,
        file_identities=moved_files,
    )
    if (
        moved_identity != expected_identity
        or moved_directories != original_directories
        or moved_files != original_files
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            "transaction directory identity changed during detachment; "
            f"the detached directory was retained at {tombstone}",
        )
    if (
        _trusted_directory_identity(parent, "transaction directory parent")
        != parent_identity
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            f"transaction directory parent identity changed during cleanup: {parent}",
        )

    if os.name != "nt":
        # POSIX exposes no portable identity-bound unlink/rmdir operation.  Preserve
        # the complete, closed transaction directory under a non-active archive name
        # instead of risking deletion of a raced replacement.
        _fsync_directory(parent)
        return

    for relative, snapshot in expected.items():
        if snapshot["exists"]:
            path = tombstone.joinpath(*PurePosixPath(relative).parts)
            _remove_snapshot_file(
                path,
                tombstone,
                f"transaction file {relative}",
                snapshot,
                expected_identity=moved_files[relative],
            )

    ordered_directories = sorted(
        moved_directories,
        key=lambda value: (len(PurePosixPath(value).parts), value),
        reverse=True,
    )
    for relative in ordered_directories:
        current = (
            tombstone
            if relative == "."
            else tombstone.joinpath(*PurePosixPath(relative).parts)
        )
        expected_current_identity = moved_directories[relative]
        current_parent = current.parent
        if relative == ".":
            expected_parent_identity = parent_identity
        else:
            parent_relative = PurePosixPath(relative).parent.as_posix()
            expected_parent_identity = moved_directories[parent_relative]
        if (
            _trusted_directory_identity(
                current_parent,
                f"detached transaction directory parent {relative}",
            )
            != expected_parent_identity
        ):
            raise Phase4Refusal(
                "recovery-conflict",
                f"detached transaction directory parent changed: {current_parent}",
            )
        try:
            _delete_empty_directory_by_handle_windows(
                current,
                expected_current_identity,
            )
        except OSError as exc:
            if _lexists(current):
                try:
                    current_info = current.lstat()
                except OSError:
                    current_info = None
                if (
                    current_info is None
                    or _is_link_or_junction(current)
                    or not stat.S_ISDIR(current_info.st_mode)
                    or _identity_from_stat(current_info) != expected_current_identity
                ):
                    raise Phase4Refusal(
                        "recovery-conflict",
                        "a foreign directory replaced a detached transaction "
                        f"directory and was retained: {current}",
                    ) from exc
            raise Phase4OperationalError(
                "recovery-cleanup-failed",
                f"cannot remove detached transaction directory {current}: {exc}",
            ) from exc
        if _lexists(current):
            current_info = current.lstat()
            if (
                _is_link_or_junction(current)
                or not stat.S_ISDIR(current_info.st_mode)
                or _identity_from_stat(current_info) != expected_current_identity
            ):
                raise Phase4Refusal(
                    "recovery-conflict",
                    "a foreign directory appeared after handle-bound deletion and "
                    f"was retained: {current}",
                )
            raise Phase4OperationalError(
                "recovery-cleanup-failed",
                f"detached transaction directory remained after deletion: {current}",
            )
        if (
            _trusted_directory_identity(
                current_parent,
                f"detached transaction directory parent {relative}",
            )
            != expected_parent_identity
        ):
            raise Phase4Refusal(
                "recovery-conflict",
                f"detached transaction directory parent changed: {current_parent}",
            )
        _fsync_directory(current_parent)


def _manifest_snapshot_paths(
    docs_root: Path,
    snapshot: Mapping[str, Any],
) -> set[str]:
    """Extract only validated document paths from a journal manifest snapshot."""

    try:
        parsed = contract.parse_json_strict(
            snapshot["text"], source="transaction manifest"
        )
        if not isinstance(parsed, Mapping):
            raise contract.ManifestSchemaError("manifest JSON root must be an object")
        schema = parsed.get("schema_version")
        if type(schema) is int and schema == contract.MANIFEST_SCHEMA_VERSION:
            normalized = contract.validate_manifest(
                dict(parsed),
                docs_root,
                verify_files=False,
                verify_complete_set=False,
            )
            return {entry["path"] for entry in normalized["files"]}
        if schema is not None and (type(schema) is not int or schema != 1):
            raise contract.ManifestSchemaError(
                f"unsupported manifest schema_version: {schema!r}"
            )
        files = parsed.get("files")
        if not isinstance(files, list):
            raise contract.ManifestSchemaError("legacy manifest files must be an array")
        paths: list[str] = []
        for index, entry in enumerate(files):
            if not isinstance(entry, Mapping):
                raise contract.ManifestSchemaError(
                    f"legacy manifest files[{index}] must be an object"
                )
            path = entry.get("path")
            paths.append(contract.validate_manifest_relative_path(path).as_posix())
        contract.ensure_unique_manifest_paths(paths)
        return set(paths)
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"journal manifest paths are invalid: {exc}",
        ) from exc


def _require_recovery_active_inventory(
    docs_root: Path,
    *,
    required: set[str],
    allowed: set[str],
    context: str,
) -> None:
    """Reject unrelated active-document changes before recovery mutates metadata."""

    active = _discover_active_documents(docs_root)
    missing = sorted(required - active)
    unexpected = sorted(active - allowed)
    if missing or unexpected:
        raise Phase4Refusal(
            "recovery-conflict",
            f"{context} active inventory changed "
            f"(missing={missing}, unexpected={unexpected})",
        )


def _validate_manifest_snapshot(docs_root: Path, snapshot: Mapping[str, Any]) -> None:
    """Require a raw journal manifest snapshot to describe the current docs tree."""

    try:
        parsed = contract.parse_json_strict(
            snapshot["text"], source="transaction manifest"
        )
        if not isinstance(parsed, Mapping):
            raise contract.ManifestSchemaError("manifest JSON root must be an object")
        contract.validate_manifest(dict(parsed), docs_root, verify_files=True)
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal(
            "recovery-conflict",
            f"journal manifest snapshot does not validate against current documents: {exc}",
        ) from exc


def _journal_manifest_relation(journal: TransactionJournal, docs_root: Path) -> str:
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    before = journal.data["manifest"]["before"]
    after = journal.data["manifest"]["after"]
    if _snapshot_matches(manifest_path, docs_root, "manifest", before):
        return "before"
    if after is not None and _snapshot_matches(
        manifest_path, docs_root, "manifest", after
    ):
        return "after"
    if not _lexists(manifest_path):
        before_artifact, after_artifact = _manifest_transaction_paths(
            docs_root,
            journal.data["transaction_id"],
        )
        if (
            journal.data["phase"] != "ready"
            or after is None
            or not _lexists(before_artifact)
            or not _lexists(after_artifact)
        ):
            raise Phase4Refusal(
                "recovery-conflict",
                "an absent canonical manifest is not a complete transaction-owned "
                "CAS gap",
            )
        _require_snapshot_state(
            before_artifact,
            docs_root,
            "manifest transaction before anchor",
            before,
            conflict_code="recovery-conflict",
        )
        _require_snapshot_state(
            after_artifact,
            docs_root,
            "manifest transaction after candidate",
            after,
            conflict_code="recovery-conflict",
        )
        try:
            _rename_no_replace(before_artifact, manifest_path)
        except FileExistsError as exc:
            raise Phase4Refusal(
                "recovery-conflict",
                "a concurrent manifest appeared while restoring the CAS gap",
            ) from exc
        except OSError as exc:
            raise Phase4OperationalError(
                "recovery-write-failed",
                f"cannot restore manifest preimage from its transaction anchor: {exc}",
            ) from exc
        _require_snapshot_state(
            manifest_path,
            docs_root,
            "restored manifest preimage",
            before,
            conflict_code="recovery-conflict",
        )
        _fsync_directory(docs_root)
        if _lexists(before_artifact):
            _remove_snapshot_file(
                before_artifact,
                docs_root,
                "manifest transaction before anchor",
                before,
                archive_root=journal.quarantine_root,
                archive_trusted_root=docs_root,
            )
        if after is not None and _lexists(after_artifact):
            _remove_snapshot_file(
                after_artifact,
                docs_root,
                "manifest transaction after candidate",
                after,
                archive_root=journal.quarantine_root,
                archive_trusted_root=docs_root,
            )
        return "before"
    raise Phase4Refusal(
        "recovery-conflict",
        "current manifest differs from both transaction journal states",
    )


def _quarantine_journal_payload_identity(
    move: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    receipt = _receipt_snapshot_data(
        move["receipt_after"],
        receipt_id=move["receipt_id"],
        original_path=move["original_path"],
        field_name="quarantine receipt.after",
    )
    return _receipt_payload_identity(
        receipt,
        error_code="recovery-journal-invalid",
        context="quarantine receipt",
    )


def _owned_or_absent_snapshot(
    path: Path,
    base: Path,
    description: str,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    """Return an expected snapshot or absence, rejecting modified transaction data."""

    current = _capture_snapshot(path, base, description)
    if current == dict(expected) or not current["exists"]:
        return current
    raise Phase4Refusal(
        "recovery-conflict",
        f"{description} differs from its transaction journal state: {path}",
    )


def _owned_or_absent_snapshot_with_identity(
    path: Path,
    base: Path,
    description: str,
    expected: Mapping[str, Any],
    expected_identity: tuple[int, int, int, int],
) -> tuple[dict[str, Any], tuple[int, int, int, int] | None]:
    current, identity = _capture_snapshot_with_identity(path, base, description)
    if not current["exists"]:
        return current, None
    if current != dict(expected) or identity != expected_identity:
        raise Phase4Refusal(
            "recovery-conflict",
            f"{description} differs from its transaction-owned content or identity: {path}",
        )
    return current, identity


def _quarantine_move_layout(
    docs_root: Path,
    quarantine_root: Path,
    move: Mapping[str, Any],
) -> dict[str, Path]:
    original = move["original_path"]
    source = _journal_document_path(
        docs_root, original, f"quarantine source {original}"
    )
    (
        final_dir,
        final_receipt,
        final_payload,
        staging_dir,
        staging_receipt,
        staging_payload,
    ) = _journal_receipt_paths(
        quarantine_root,
        move["receipt_id"],
        original,
        move["staging_name"],
    )
    if (
        staging_receipt is None or staging_payload is None
    ):  # pragma: no cover - fixed call above
        raise Phase4Refusal(
            "recovery-journal-invalid", "quarantine staging layout is incomplete"
        )
    return {
        "source": source,
        "final_dir": final_dir,
        "final_receipt": final_receipt,
        "final_payload": final_payload,
        "staging_dir": staging_dir,
        "staging_receipt": staging_receipt,
        "staging_payload": staging_payload,
    }


def _restore_layout(
    docs_root: Path,
    quarantine_root: Path,
    restore: Mapping[str, Any],
) -> dict[str, Path]:
    target = _journal_document_path(
        docs_root,
        restore["original_path"],
        f"restore target {restore['original_path']}",
    )
    final_dir, receipt_path, payload_path, _, _, _ = _journal_receipt_paths(
        quarantine_root,
        restore["receipt_id"],
        restore["original_path"],
    )
    return {
        "target": target,
        "receipt_dir": final_dir,
        "receipt": receipt_path,
        "payload": payload_path,
        "anchor": _restore_anchor_path(
            quarantine_root,
            restore["receipt_id"],
        ),
    }


def _restore_journal_target_identity(
    restore: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    """Extract the independent restored target identity from journal schema v2."""

    try:
        return _validate_serialized_identity(
            restore.get("target_identity"),
            "restore.target_identity",
        )
    except Phase4Refusal as exc:
        raise Phase4Refusal("recovery-journal-invalid", exc.message) from exc


def _restore_journal_payload_identity(
    restore: Mapping[str, Any],
) -> tuple[int, int, int, int]:
    """Extract the persisted payload identity from a restore journal receipt."""

    receipt = _receipt_snapshot_data(
        restore["receipt_before"],
        receipt_id=restore["receipt_id"],
        original_path=restore["original_path"],
        field_name="restore.receipt_before",
    )
    return _receipt_payload_identity(
        receipt,
        error_code="recovery-journal-invalid",
        context="restore receipt",
    )


def _require_restore_recovery_receipt_state(
    journal: TransactionJournal,
    layout: Mapping[str, Path],
) -> None:
    """Validate the complete receipt closed set before recovery writes metadata."""

    restore = journal.data["restore"]
    allowed_receipts = [restore["receipt_before"]]
    if journal.data["phase"] == "ready":
        allowed_receipts.append(restore["receipt_after"])
    receipt_current, _ = _capture_snapshot_with_identity(
        layout["receipt"],
        journal.quarantine_root,
        f"restore receipt {restore['receipt_id']}",
    )
    if receipt_current not in allowed_receipts:
        complete_gap = False
        if journal.data["phase"] == "ready" and receipt_current == _absent_snapshot():
            docs_root = journal.quarantine_root.parent
            complete_gap = True
            for snapshot in (restore["receipt_before"], restore["receipt_after"]):
                artifact = _metadata_snapshot_artifact_path(
                    journal.quarantine_root,
                    docs_root,
                    layout["receipt"],
                    snapshot,
                )
                if not _lexists(artifact):
                    complete_gap = False
                    break
                _require_snapshot_state(
                    artifact,
                    docs_root,
                    "restore receipt metadata artifact",
                    snapshot,
                    conflict_code="recovery-conflict",
                )
        if not complete_gap:
            raise Phase4Refusal(
                "recovery-conflict",
                "restore receipt differs from every journal-owned recovery state",
            )
    payload_current, payload_identity = _capture_snapshot_with_identity(
        layout["payload"],
        journal.quarantine_root,
        f"restore payload {restore['receipt_id']}",
    )
    expected_payload_identity = _restore_journal_payload_identity(restore)
    if (
        payload_current != restore["payload_before"]
        or payload_identity != expected_payload_identity
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            "restore payload differs from its journal-owned content or identity",
        )
    member_identities: dict[str, tuple[int, int, int, int]] = {}
    _require_closed_transaction_directory(
        layout["receipt_dir"],
        journal.quarantine_root,
        {
            RECEIPT_FILENAME: receipt_current,
            f"{PAYLOAD_DIRNAME}/{restore['original_path']}": payload_current,
        },
        file_identities=member_identities,
    )
    if (
        member_identities.get(f"{PAYLOAD_DIRNAME}/{restore['original_path']}")
        != expected_payload_identity
    ):
        raise Phase4Refusal(
            "recovery-conflict",
            "restore payload identity changed during receipt validation",
        )


def _restore_source_from_quarantine(
    source: Path,
    docs_root: Path,
    description: str,
    source_before: Mapping[str, Any],
    payloads: Sequence[Path],
) -> None:
    """Restore an absent active document from an intact payload or journal text."""

    current = _capture_snapshot(source, docs_root, description)
    if current == dict(source_before):
        return
    if current["exists"]:
        raise Phase4Refusal(
            "recovery-conflict",
            f"{description} exists with unrecognized content: {source}",
        )
    # A journal contains the exact original text so recovery remains possible even
    # if a crash happened between rename and a staging receipt write.  Payloads
    # are independently checked by the caller before their directories are cleaned.
    del payloads
    _write_exclusive_snapshot(source, docs_root, description, source_before)


def _source_is_absent(path: Path, docs_root: Path, description: str) -> None:
    snapshot = _capture_snapshot(path, docs_root, description)
    if snapshot["exists"]:
        raise Phase4Refusal(
            "recovery-conflict",
            f"{description} unexpectedly exists after the manifest commit: {path}",
        )


def _build_recovery_manifest(
    docs_root: Path, before_snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a new v2 manifest only when a prepared restore must roll forward."""

    try:
        parsed = contract.parse_json_strict(
            before_snapshot["text"], source="journal manifest.before"
        )
        if not isinstance(parsed, Mapping):
            raise contract.ManifestSchemaError("manifest JSON root must be an object")
        previous = contract.validate_manifest(
            dict(parsed),
            docs_root,
            verify_files=True,
            verify_complete_set=False,
        )
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal(
            "recovery-conflict",
            f"cannot derive restore manifest from journal before state: {exc}",
        ) from exc
    producer_value = previous.get("producer")
    producer: Mapping[str, str] | None = None
    if isinstance(producer_value, Mapping):
        name = producer_value.get("name")
        version = producer_value.get("version")
        if isinstance(name, str) and name and isinstance(version, str) and version:
            producer = {"name": name, "version": version}
    label = previous.get("docs_root")
    docs_root_label = label if isinstance(label, str) and label else docs_root.name
    return contract.build_manifest(
        docs_root,
        producer=producer,
        last_synced=_utc_now(),
        docs_root_label=docs_root_label,
    )


def _ready_restore_journal_from_target(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> TransactionJournal:
    """Publish ready metadata from a persisted target-created generation."""

    if journal.data["phase"] != "target-created":
        return journal
    before_paths = _manifest_snapshot_paths(
        docs_root,
        journal.data["manifest"]["before"],
    )
    restored_path = journal.data["restore"]["original_path"]
    if restored_path in before_paths:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "restore target was already registered in manifest.before",
        )
    expected_paths = before_paths | {restored_path}
    _require_recovery_active_inventory(
        docs_root,
        required=expected_paths,
        allowed=expected_paths,
        context="prepared restore",
    )
    restore = journal.data["restore"]
    layout = _restore_layout(docs_root, journal.quarantine_root, restore)
    _require_snapshot_state(
        layout["target"],
        docs_root,
        f"restore target {restored_path}",
        restore["target_after"],
        conflict_code="recovery-conflict",
    )
    _require_restore_target_identity(
        layout["target"],
        docs_root,
        _restore_journal_target_identity(restore),
        layout["anchor"],
        journal.quarantine_root,
        code="recovery-conflict",
    )
    manifest_after = _build_recovery_manifest(
        docs_root, journal.data["manifest"]["before"]
    )
    claude_path = root / "CLAUDE.md"
    claude_before = journal.data["claude"]["before"]
    _require_snapshot_state(claude_path, root, "CLAUDE.md", claude_before)
    claude_update = _prepare_claude_update(root, manifest_after)
    claude_after = (
        dict(claude_before)
        if claude_update is None
        else _text_snapshot(claude_update.after)
    )
    return _update_transaction_journal(
        journal,
        _journal_with_metadata(
            journal,
            manifest_after=manifest_after,
            claude_after=claude_after,
        ),
    )


def _recover_quarantine_before_manifest(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> None:
    """Rollback all known moves while the journal manifest remains the before state."""

    quarantine = journal.data["quarantine"]
    before_paths = _manifest_snapshot_paths(
        docs_root,
        journal.data["manifest"]["before"],
    )
    candidate_paths = {move["original_path"] for move in quarantine["moves"]}
    if not candidate_paths <= before_paths:
        raise Phase4Refusal(
            "recovery-journal-invalid",
            "quarantine candidates are absent from manifest.before",
        )
    _require_recovery_active_inventory(
        docs_root,
        required=before_paths - candidate_paths,
        allowed=before_paths,
        context="uncommitted quarantine",
    )
    for move in reversed(quarantine["moves"]):
        layout = _quarantine_move_layout(docs_root, journal.quarantine_root, move)
        source_before = move["source_before"]
        payload_identity = _quarantine_journal_payload_identity(move)
        final_payload, _ = _owned_or_absent_snapshot_with_identity(
            layout["final_payload"],
            journal.quarantine_root,
            f"quarantine payload {move['receipt_id']}",
            source_before,
            payload_identity,
        )
        staging_payload, _ = _owned_or_absent_snapshot_with_identity(
            layout["staging_payload"],
            journal.quarantine_root,
            f"quarantine staging payload {move['receipt_id']}",
            source_before,
            payload_identity,
        )
        final_receipt = _owned_or_absent_snapshot(
            layout["final_receipt"],
            journal.quarantine_root,
            f"quarantine receipt {move['receipt_id']}",
            move["receipt_after"],
        )
        staging_receipt = _owned_or_absent_snapshot(
            layout["staging_receipt"],
            journal.quarantine_root,
            f"quarantine staging receipt {move['receipt_id']}",
            move["receipt_after"],
        )
        _restore_source_from_quarantine(
            layout["source"],
            docs_root,
            f"quarantine source {move['original_path']}",
            source_before,
            [layout["final_payload"], layout["staging_payload"]],
        )
        payload_relative = f"{PAYLOAD_DIRNAME}/{move['original_path']}"
        _safe_remove_transaction_directory(
            layout["final_dir"],
            journal.quarantine_root,
            {
                RECEIPT_FILENAME: final_receipt,
                payload_relative: final_payload,
            },
            expected_file_identities=(
                {payload_relative: payload_identity} if final_payload["exists"] else {}
            ),
        )
        _safe_remove_transaction_directory(
            layout["staging_dir"],
            journal.quarantine_root,
            {
                RECEIPT_FILENAME: staging_receipt,
                payload_relative: staging_payload,
            },
            expected_file_identities=(
                {payload_relative: payload_identity}
                if staging_payload["exists"]
                else {}
            ),
        )
    claude_path = root / "CLAUDE.md"
    claude_before = journal.data["claude"]["before"]
    claude_after = journal.data["claude"]["after"]
    allowed = [claude_before] if claude_after is None else [claude_before, claude_after]
    _set_snapshot_state(
        claude_path,
        root,
        "CLAUDE.md",
        claude_before,
        allowed,
        archive_root=journal.quarantine_root,
        archive_trusted_root=root,
    )
    _validate_manifest_snapshot(docs_root, journal.data["manifest"]["before"])


def _recover_quarantine_after_manifest(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> None:
    """Finish a committed quarantine without touching active documents."""

    manifest_after = journal.data["manifest"]["after"]
    if manifest_after is None:
        raise Phase4Refusal(
            "recovery-conflict",
            "quarantine manifest committed before an after journal state existed",
        )
    _validate_manifest_snapshot(docs_root, manifest_after)
    for move in journal.data["quarantine"]["moves"]:
        layout = _quarantine_move_layout(docs_root, journal.quarantine_root, move)
        _source_is_absent(
            layout["source"],
            docs_root,
            f"quarantined active document {move['original_path']}",
        )
        payload_identity = _quarantine_journal_payload_identity(move)
        payload_current, _ = _owned_or_absent_snapshot_with_identity(
            layout["final_payload"],
            journal.quarantine_root,
            f"quarantine payload {move['receipt_id']}",
            move["source_before"],
            payload_identity,
        )
        receipt_current = _owned_or_absent_snapshot(
            layout["final_receipt"],
            journal.quarantine_root,
            f"quarantine receipt {move['receipt_id']}",
            move["receipt_after"],
        )
        if not payload_current["exists"] or not receipt_current["exists"]:
            raise Phase4Refusal(
                "recovery-conflict",
                "committed quarantine receipt or payload disappeared after publication",
            )
        payload_relative = f"{PAYLOAD_DIRNAME}/{move['original_path']}"
        file_identities: dict[str, tuple[int, int, int, int]] = {}
        _require_closed_transaction_directory(
            layout["final_dir"],
            journal.quarantine_root,
            {
                RECEIPT_FILENAME: receipt_current,
                payload_relative: payload_current,
            },
            file_identities=file_identities,
        )
        if file_identities.get(payload_relative) != payload_identity:
            raise Phase4Refusal(
                "recovery-conflict",
                "committed quarantine payload identity changed",
            )
        staging_payload, _ = _owned_or_absent_snapshot_with_identity(
            layout["staging_payload"],
            journal.quarantine_root,
            f"quarantine staging payload {move['receipt_id']}",
            move["source_before"],
            payload_identity,
        )
        staging_receipt = _owned_or_absent_snapshot(
            layout["staging_receipt"],
            journal.quarantine_root,
            f"quarantine staging receipt {move['receipt_id']}",
            move["receipt_after"],
        )
        _safe_remove_transaction_directory(
            layout["staging_dir"],
            journal.quarantine_root,
            {
                RECEIPT_FILENAME: staging_receipt,
                payload_relative: staging_payload,
            },
            expected_file_identities=(
                {payload_relative: payload_identity}
                if staging_payload["exists"]
                else {}
            ),
        )
    claude_path = root / "CLAUDE.md"
    claude_before = journal.data["claude"]["before"]
    claude_after = journal.data["claude"]["after"]
    if claude_after is None:
        raise Phase4Refusal(
            "recovery-conflict", "committed quarantine journal lacks CLAUDE after state"
        )
    _set_snapshot_state(
        claude_path,
        root,
        "CLAUDE.md",
        claude_after,
        [claude_before, claude_after],
        archive_root=journal.quarantine_root,
        archive_trusted_root=root,
    )
    _validate_manifest_snapshot(docs_root, manifest_after)


def _recover_restore_cancel(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> None:
    """Cancel an uncommitted restore that never created its target document."""

    if journal.data["phase"] != "prepared":
        raise Phase4Refusal(
            "recovery-conflict",
            "only a prepared restore without a staged target can be cancelled",
        )
    _validate_manifest_snapshot(docs_root, journal.data["manifest"]["before"])
    restore = journal.data["restore"]
    layout = _restore_layout(docs_root, journal.quarantine_root, restore)
    _require_snapshot_state(
        layout["target"],
        docs_root,
        f"restore target {restore['original_path']}",
        restore["target_before"],
    )
    _set_snapshot_state(
        layout["receipt"],
        journal.quarantine_root,
        f"restore receipt {restore['receipt_id']}",
        restore["receipt_before"],
        [restore["receipt_before"], restore["receipt_after"]],
        archive_root=journal.quarantine_root,
        archive_trusted_root=docs_root,
    )
    _set_snapshot_state(
        layout["payload"],
        journal.quarantine_root,
        f"restore payload {restore['receipt_id']}",
        restore["payload_before"],
        [restore["payload_before"], restore["payload_after"]],
    )
    _require_snapshot_state(
        layout["anchor"],
        journal.quarantine_root,
        f"restore staged target {restore['receipt_id']}",
        _absent_snapshot(),
        conflict_code="recovery-conflict",
    )
    _require_closed_transaction_directory(
        layout["receipt_dir"],
        journal.quarantine_root,
        {
            RECEIPT_FILENAME: restore["receipt_before"],
            f"{PAYLOAD_DIRNAME}/{restore['original_path']}": restore["payload_before"],
        },
    )
    claude_path = root / "CLAUDE.md"
    claude_before = journal.data["claude"]["before"]
    claude_after = journal.data["claude"]["after"]
    allowed = [claude_before] if claude_after is None else [claude_before, claude_after]
    _set_snapshot_state(
        claude_path,
        root,
        "CLAUDE.md",
        claude_before,
        allowed,
        archive_root=journal.quarantine_root,
        archive_trusted_root=root,
    )
    _validate_manifest_snapshot(docs_root, journal.data["manifest"]["before"])


def _complete_restore_rollforward(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> None:
    """Publish exact restore metadata, retain no duplicate payload, then validate."""

    if journal.data["phase"] != "ready":
        raise Phase4Refusal(
            "recovery-conflict",
            "restore cannot roll forward without ready journal metadata",
        )
    restore = journal.data["restore"]
    layout = _restore_layout(docs_root, journal.quarantine_root, restore)
    target_identity = _restore_journal_target_identity(restore)
    _require_restore_recovery_receipt_state(journal, layout)
    _require_snapshot_state(
        layout["target"],
        docs_root,
        f"restore target {restore['original_path']}",
        restore["target_after"],
    )
    _require_restore_target_identity(
        layout["target"],
        docs_root,
        target_identity,
        layout["anchor"],
        journal.quarantine_root,
        code="recovery-conflict",
    )
    claude_path = root / "CLAUDE.md"
    claude_before = journal.data["claude"]["before"]
    claude_after = journal.data["claude"]["after"]
    manifest_before = journal.data["manifest"]["before"]
    manifest_after = journal.data["manifest"]["after"]
    if claude_after is None or manifest_after is None:
        raise Phase4Refusal(
            "recovery-conflict", "ready restore journal lacks metadata after state"
        )
    _validate_manifest_snapshot(docs_root, manifest_after)
    _set_snapshot_state(
        claude_path,
        root,
        "CLAUDE.md",
        claude_after,
        [claude_before, claude_after],
        archive_root=journal.quarantine_root,
        archive_trusted_root=root,
    )
    _set_snapshot_state(
        layout["receipt"],
        journal.quarantine_root,
        f"restore receipt {restore['receipt_id']}",
        restore["receipt_after"],
        [restore["receipt_before"], restore["receipt_after"]],
        archive_root=journal.quarantine_root,
        archive_trusted_root=docs_root,
    )
    _require_restore_recovery_receipt_state(journal, layout)
    manifest_path = docs_root / contract.MANIFEST_FILENAME

    def validate_recovery_restore_boundary(
        expected_manifest_snapshot: Mapping[str, Any],
    ) -> None:
        _require_snapshot_state(
            manifest_path,
            docs_root,
            "manifest",
            expected_manifest_snapshot,
            conflict_code="recovery-conflict",
        )
        _validate_manifest_snapshot(docs_root, manifest_after)
        _require_snapshot_state(
            layout["target"],
            docs_root,
            f"restore target {restore['original_path']}",
            restore["target_after"],
            conflict_code="recovery-conflict",
        )
        _require_snapshot_state(
            layout["payload"],
            journal.quarantine_root,
            f"restore payload {restore['receipt_id']}",
            restore["payload_before"],
            conflict_code="recovery-conflict",
        )
        _require_restore_target_identity(
            layout["target"],
            docs_root,
            target_identity,
            layout["anchor"],
            journal.quarantine_root,
            code="recovery-conflict",
        )
        _require_restore_recovery_receipt_state(journal, layout)
        _require_snapshot_state(
            claude_path,
            root,
            "CLAUDE.md",
            claude_after,
            conflict_code="recovery-conflict",
        )
        _require_snapshot_state(
            journal.path,
            journal.quarantine_root,
            "transaction journal",
            journal.file_snapshot,
            conflict_code="recovery-journal-conflict",
        )

    if _snapshot_matches(manifest_path, docs_root, "manifest", manifest_before):
        try:
            parsed_manifest = contract.parse_json_strict(
                manifest_after["text"],
                source="restore journal manifest.after",
            )
        except contract.ProjectDocsContractError as exc:
            raise Phase4Refusal("recovery-journal-invalid", str(exc)) from exc
        if not isinstance(parsed_manifest, Mapping):
            raise Phase4Refusal(
                "recovery-journal-invalid",
                "restore journal manifest.after is not an object",
            )
        disposition = _commit_manifest(
            manifest_path,
            parsed_manifest,
            journal,
            precommit_validator=lambda: validate_recovery_restore_boundary(
                manifest_before
            ),
            postcommit_validator=lambda: validate_recovery_restore_boundary(
                manifest_after
            ),
        )
        if disposition is not ManifestCommitDisposition.DURABLE:
            raise Phase4OperationalError(
                "manifest-durability-unknown",
                f"manifest commit disposition is {disposition.value}",
            )
    elif not _snapshot_matches(manifest_path, docs_root, "manifest", manifest_after):
        raise Phase4Refusal(
            "recovery-conflict",
            "manifest differs from both restore journal generations",
        )
    _validate_manifest_snapshot(docs_root, manifest_after)
    _require_snapshot_state(
        layout["payload"],
        journal.quarantine_root,
        f"restore payload {restore['receipt_id']}",
        restore["payload_before"],
        conflict_code="recovery-conflict",
    )
    _require_restore_target_identity(
        layout["target"],
        docs_root,
        target_identity,
        layout["anchor"],
        journal.quarantine_root,
        code="recovery-conflict",
    )
    _require_restore_recovery_receipt_state(journal, layout)


def _recover_quarantine_transaction(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> str:
    relation = _journal_manifest_relation(journal, docs_root)
    if relation == "before":
        _recover_quarantine_before_manifest(journal, root, docs_root)
        return "quarantine-rolled-back"
    _recover_quarantine_after_manifest(journal, root, docs_root)
    return "quarantine-finalized"


def _recover_restore_transaction(
    journal: TransactionJournal,
    root: Path,
    docs_root: Path,
) -> str:
    restore = journal.data["restore"]
    layout = _restore_layout(docs_root, journal.quarantine_root, restore)
    _require_restore_recovery_receipt_state(journal, layout)
    relation = _journal_manifest_relation(journal, docs_root)
    target, current_target_identity = _capture_snapshot_with_identity(
        layout["target"],
        docs_root,
        f"restore target {restore['original_path']}",
    )
    staged, current_staged_identity = _capture_snapshot_with_identity(
        layout["anchor"],
        journal.quarantine_root,
        f"restore staged target {restore['receipt_id']}",
    )

    phase = journal.data["phase"]
    if phase == "prepared":
        if relation != "before":
            raise Phase4Refusal(
                "recovery-conflict",
                "prepared restore cannot coexist with a committed manifest",
            )
        if target["exists"] or staged["exists"]:
            raise Phase4Refusal(
                "recovery-unowned-staged-target",
                "prepared restore found a target or staged inode whose identity was "
                "never durably recorded; automatic recovery is unsafe",
            )
        _recover_restore_cancel(journal, root, docs_root)
        return "restore-cancelled"

    target_identity = _restore_journal_target_identity(restore)
    payload_identity = _restore_journal_payload_identity(restore)
    if target_identity == payload_identity:
        raise Phase4Refusal(
            "recovery-conflict",
            "restore target identity aliases the quarantine payload identity",
        )
    _require_snapshot_state(
        layout["payload"],
        journal.quarantine_root,
        f"restore payload {restore['receipt_id']}",
        restore["payload_before"],
        conflict_code="recovery-conflict",
    )

    if phase == "target-created":
        if relation != "before":
            raise Phase4Refusal(
                "recovery-conflict",
                "target-created restore cannot coexist with a committed manifest",
            )
        if target["exists"] == staged["exists"]:
            raise Phase4Refusal(
                "recovery-conflict",
                "target-created restore requires exactly one of its staged and active paths",
            )
        if staged["exists"]:
            if (
                staged != restore["target_after"]
                or current_staged_identity != target_identity
            ):
                raise Phase4Refusal(
                    "recovery-conflict",
                    "restore staged target differs from its persisted identity or content",
                )
            _require_snapshot_state(
                layout["target"],
                docs_root,
                f"restore target {restore['original_path']}",
                restore["target_before"],
                conflict_code="recovery-conflict",
            )
            _validate_manifest_snapshot(docs_root, journal.data["manifest"]["before"])
            try:
                _exclusive_copy_file(
                    layout["anchor"],
                    layout["target"],
                    restore["target_after"]["sha256"],
                    target_identity,
                    archive_root=journal.quarantine_root,
                    archive_trusted_root=docs_root,
                    cleanup_on_failure=False,
                )
            except FileExistsError as exc:
                raise Phase4Refusal(
                    "recovery-conflict",
                    "restore target appeared while publishing its persisted staged inode",
                ) from exc
            except (Phase4Refusal, Phase4OperationalError):
                raise
            except OSError as exc:
                raise Phase4OperationalError(
                    "recovery-write-failed",
                    f"cannot publish persisted restore target: {exc}",
                ) from exc
        else:
            if (
                target != restore["target_after"]
                or current_target_identity != target_identity
            ):
                raise Phase4Refusal(
                    "recovery-conflict",
                    "restore target differs from its persisted identity or content",
                )
            _require_snapshot_state(
                layout["anchor"],
                journal.quarantine_root,
                f"restore staged target {restore['receipt_id']}",
                _absent_snapshot(),
                conflict_code="recovery-conflict",
            )
        journal = _ready_restore_journal_from_target(journal, root, docs_root)
    elif phase != "ready":  # pragma: no cover - validated journal contract
        raise Phase4Refusal(
            "recovery-journal-invalid",
            f"unsupported restore journal phase: {phase}",
        )

    _require_restore_target_identity(
        layout["target"],
        docs_root,
        target_identity,
        layout["anchor"],
        journal.quarantine_root,
        code="recovery-conflict",
    )
    _complete_restore_rollforward(journal, root, docs_root)
    return "restore-finalized"


def _recover_pending_transaction(
    root_value: str | os.PathLike[str],
) -> tuple[dict[str, Any], int]:
    """Recover one pending transaction inside the shared writer lock."""

    root, docs_root = _resolve_project(root_value)
    try:
        with contract.project_sync_lock(root, timeout=10.0):
            coordinator_status = contract.coordinator_pending_publish_status(root)
            if coordinator_status is not None:
                return _coordinator_recovery_required_result(
                    "recover",
                    "recovery",
                    coordinator_status,
                )
            journal = _pending_transaction(root, recover_publication_temp=True)
            if journal is None:
                result = _empty_result("recover", "recovery")
                result.update(
                    {
                        "ok": True,
                        "status": "no-pending-transaction",
                        "recovery": {"required": False, "recovered": False},
                    }
                )
                return result, EXIT_OK
            _require_no_archive_active_alias(
                docs_root,
                journal.quarantine_root,
                _discover_active_documents(docs_root),
            )
            initial_journal_identity = _journal_snapshot_identity(
                journal,
                "transaction journal selected for recovery",
            )
            if journal.data["operation"] == "quarantine":
                outcome = _recover_quarantine_transaction(journal, root, docs_root)
            else:
                outcome = _recover_restore_transaction(journal, root, docs_root)
            try:
                _fsync_directory(docs_root)
            except OSError as exc:
                raise Phase4OperationalError(
                    "recovery-durability-failed",
                    f"cannot confirm recovered project_docs durability: {exc}",
                ) from exc
            completed_journal = _pending_transaction(root)
            if (
                completed_journal is None
                or completed_journal.data["transaction_id"]
                != journal.data["transaction_id"]
            ):
                raise Phase4Refusal(
                    "recovery-journal-conflict",
                    "transaction journal changed while recovery was completing",
                )
            if completed_journal.path != journal.path:
                _require_ready_journal_transition(journal, completed_journal)
            else:
                if (
                    completed_journal.data != journal.data
                    or completed_journal.file_snapshot != journal.file_snapshot
                    or _journal_snapshot_identity(
                        completed_journal,
                        "completed transaction journal",
                    )
                    != initial_journal_identity
                ):
                    raise Phase4Refusal(
                        "recovery-journal-conflict",
                        "the selected transaction journal generation was replaced "
                        "while recovery was completing",
                    )
            _journal_manifest_relation(completed_journal, docs_root)
            _clear_transaction_journal(completed_journal)
            cleanup_warnings: list[str] = []
            if outcome == "restore-finalized":
                restore = completed_journal.data["restore"]
                layout = _restore_layout(
                    docs_root,
                    completed_journal.quarantine_root,
                    restore,
                )
                payload_identity = _restore_journal_payload_identity(restore)
                try:
                    _require_snapshot_state(
                        layout["anchor"],
                        completed_journal.quarantine_root,
                        f"restore staged target {restore['receipt_id']}",
                        _absent_snapshot(),
                        conflict_code="recovery-conflict",
                    )
                    _remove_snapshot_file(
                        layout["payload"],
                        completed_journal.quarantine_root,
                        f"restored duplicate payload {restore['receipt_id']}",
                        restore["payload_before"],
                        expected_identity=payload_identity,
                        archive_root=completed_journal.quarantine_root,
                        archive_trusted_root=docs_root,
                    )
                except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
                    cleanup_warnings.append(
                        "recovery completed, but retained a duplicate payload for "
                        f"later no-op cleanup: {cleanup_exc.message}"
                    )
            result = _empty_result("recover", "recovery")
            result.update(
                {
                    "ok": True,
                    "status": "recovered",
                    "recovery": {
                        "required": False,
                        "recovered": True,
                        "outcome": outcome,
                        "operation": completed_journal.data["operation"],
                        "transaction_id": completed_journal.data["transaction_id"],
                    },
                }
            )
            result["warnings"].extend(cleanup_warnings)
            return result, EXIT_OK
    except contract.LockError as exc:
        raise Phase4OperationalError(
            "project-sync-lock-failed",
            f"cannot acquire or release the project sync lock: {exc}",
        ) from exc


def _recovery_required_result(
    action: str,
    mode: str,
    journal: TransactionJournal | None,
    error: Phase4Refusal | None = None,
) -> tuple[dict[str, Any], int]:
    """Return the stable writer-blocking result without touching scan state."""

    result = _empty_result(action, mode)
    recovery: dict[str, Any] = {"required": True}
    message = "a pending Phase 4 transaction must be recovered with --recover"
    if journal is not None:
        recovery.update(
            {
                "operation": journal.data["operation"],
                "transaction_id": journal.data["transaction_id"],
                "journal": journal.path.name,
            }
        )
    if error is not None:
        recovery["detail"] = error.message
        message = f"{message}: {error.message}"
    result.update({"status": "phase4-recovery-required", "recovery": recovery})
    result["errors"].append({"code": "phase4-recovery-required", "message": message})
    return result, EXIT_REFUSED


def _coordinator_recovery_required_result(
    action: str,
    mode: str,
    status: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    """Block ordinary Phase 4 actions behind a coordinator transaction."""

    result = _empty_result(action, mode)
    message = (
        "a pending coordinator publish transaction must be recovered with "
        "sync_project_docs.py --recover"
    )
    result.update(
        {
            "status": "coordinator-recovery-required",
            "coordinator_recovery": copy.deepcopy(dict(status)),
        }
    )
    result["errors"].append(
        {"code": "coordinator-recovery-required", "message": message}
    )
    return result, EXIT_REFUSED


def pending_recovery_status(
    root_value: str | os.PathLike[str],
) -> dict[str, Any] | None:
    """Expose pending-journal detection for report and coordinator gates."""

    journal = _pending_transaction(root_value)
    if journal is None:
        return None
    return {
        "required": True,
        "operation": journal.data["operation"],
        "transaction_id": journal.data["transaction_id"],
        "journal": journal.path.name,
    }


def _block_if_pending_recovery(
    root_value: str | os.PathLike[str],
    action: str,
    mode: str,
) -> tuple[dict[str, Any], int] | None:
    try:
        journal = _pending_transaction(root_value)
    except Phase4Refusal as exc:
        return _recovery_required_result(action, mode, None, exc)
    if journal is not None:
        return _recovery_required_result(action, mode, journal)
    try:
        coordinator_status = contract.coordinator_pending_publish_status(root_value)
    except contract.LockError as exc:
        coordinator_status = {
            "required": True,
            "detail": str(exc),
        }
    if coordinator_status is not None:
        return _coordinator_recovery_required_result(
            action,
            mode,
            coordinator_status,
        )
    return None


def _discover_active_documents(docs_root: Path) -> set[str]:
    """使用共享契约枚举完整活动 Markdown 集合。"""

    try:
        return set(contract.discover_active_markdown_paths(docs_root))
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("untrusted-path", str(exc)) from exc


def _regular_file_identity(
    path: Path,
    trusted_root: Path,
    description: str,
) -> tuple[int, int, int, int]:
    """Capture one direct regular-file identity without reading mutable bytes."""

    _validate_existing_components(trusted_root, path, description)
    if _is_link_or_junction(path):
        raise Phase4Refusal(
            "phase4-archive-inventory-unsafe",
            f"{description} is indirect: {path}",
        )
    try:
        info = path.lstat()
    except OSError as exc:
        raise Phase4Refusal(
            "phase4-archive-inventory-unsafe",
            f"cannot inspect {description}: {path}: {exc}",
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise Phase4Refusal(
            "phase4-archive-inventory-unsafe",
            f"{description} is not a regular file: {path}",
        )
    return _identity_from_stat(info)


def _scan_retained_archive_identities(
    quarantine_root: Path,
) -> dict[str, tuple[int, int, int, int]]:
    """Enumerate files below every retained ``.phase4-archive-*`` entry."""

    root_identity = _trusted_directory_identity(
        quarantine_root,
        "quarantine archive inventory root",
    )
    directories: dict[Path, tuple[int, int, int, int]] = {}
    archives: dict[str, tuple[int, int, int, int]] = {}
    pending: list[tuple[Path, bool]] = [(quarantine_root, False)]
    while pending:
        current, inside_archive = pending.pop()
        directories[current] = _trusted_directory_identity(
            current,
            "quarantine archive inventory directory",
        )
        try:
            entries = sorted(os.scandir(current), key=lambda entry: entry.name)
        except OSError as exc:
            raise Phase4Refusal(
                "phase4-archive-inventory-unsafe",
                f"cannot enumerate quarantine archive inventory {current}: {exc}",
            ) from exc
        for entry in entries:
            child = Path(entry.path)
            relative = child.relative_to(quarantine_root).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise Phase4Refusal(
                    "phase4-archive-inventory-unsafe",
                    f"cannot inspect quarantine member {child}: {exc}",
                ) from exc
            is_reparse = bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if entry.is_symlink() or is_reparse:
                raise Phase4Refusal(
                    "phase4-archive-inventory-unsafe",
                    f"quarantine archive inventory contains an indirect member: {child}",
                )
            archived = inside_archive or entry.name.startswith(_ARCHIVE_PREFIX)
            if stat.S_ISDIR(info.st_mode):
                pending.append((child, archived))
            elif stat.S_ISREG(info.st_mode):
                if archived:
                    archives[relative] = _regular_file_identity(
                        child,
                        quarantine_root,
                        f"retained archive file {relative}",
                    )
            elif archived:
                raise Phase4Refusal(
                    "phase4-archive-inventory-unsafe",
                    f"retained archive contains a non-regular member: {child}",
                )

    for directory, expected_identity in directories.items():
        if (
            _trusted_directory_identity(
                directory,
                "quarantine archive inventory directory",
            )
            != expected_identity
        ):
            raise Phase4Refusal(
                "phase4-archive-inventory-changed",
                f"quarantine archive directory changed during inspection: {directory}",
            )
    if (
        _trusted_directory_identity(
            quarantine_root,
            "quarantine archive inventory root",
        )
        != root_identity
    ):
        raise Phase4Refusal(
            "phase4-archive-inventory-changed",
            "quarantine archive inventory root changed during inspection",
        )
    return archives


def _require_no_archive_active_alias(
    docs_root: Path,
    quarantine_root: Path,
    active_paths: set[str],
) -> None:
    """Reject historical archives that still alias an active document inode."""

    first_archives = _scan_retained_archive_identities(quarantine_root)
    second_archives = _scan_retained_archive_identities(quarantine_root)
    if first_archives != second_archives:
        raise Phase4Refusal(
            "phase4-archive-inventory-changed",
            "retained archive inventory changed during alias validation",
        )
    if not first_archives or not active_paths:
        return

    active_documents: dict[str, Path] = {}
    active_identities: dict[str, tuple[int, int, int, int]] = {}
    try:
        for relative in sorted(active_paths):
            document = contract.resolve_manifest_document(
                docs_root,
                relative,
                must_exist=True,
            ).resolved_path
            active_documents[relative] = document
            active_identities[relative] = _regular_file_identity(
                document,
                docs_root,
                f"active manifest document {relative}",
            )
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("untrusted-path", str(exc)) from exc

    archive_objects: dict[tuple[int, int], list[str]] = {}
    for relative, identity in first_archives.items():
        archive_objects.setdefault(identity[:2], []).append(relative)
    conflicts = [
        (relative, sorted(archive_objects[identity[:2]]))
        for relative, identity in active_identities.items()
        if identity[:2] in archive_objects
    ]
    if conflicts:
        detail = "; ".join(
            f"{active} aliases {', '.join(archives)}" for active, archives in conflicts
        )
        raise Phase4Refusal(
            "phase4-archive-active-alias",
            "a retained Phase 4 archive aliases an active manifest document: " + detail,
        )

    repeated_active = {
        relative: _regular_file_identity(
            active_documents[relative],
            docs_root,
            f"active manifest document {relative}",
        )
        for relative in sorted(active_paths)
    }
    if repeated_active != active_identities:
        raise Phase4Refusal(
            "active-inventory-changed",
            "active document identities changed during archive alias validation",
        )
    final_archives = _scan_retained_archive_identities(quarantine_root)
    if final_archives != first_archives:
        raise Phase4Refusal(
            "phase4-archive-inventory-changed",
            "retained archive inventory changed during alias validation",
        )


def _should_prune_source(name: str) -> bool:
    return name.casefold() in _SOURCE_EXCLUSIONS_FOLDED or name.startswith(".")


def _discover_source_directories(root: Path, docs_root: Path) -> SourceInventory:
    """Walk source directories, pruning descendants of non-root boundaries."""

    directories: set[str] = set()
    boundaries: set[str] = set()
    casefold_paths: dict[str, str] = {}
    walk_errors: list[OSError] = []
    visited = {os.path.normcase(os.fspath(root))}

    def onerror(error: OSError) -> None:
        walk_errors.append(error)

    for directory, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
        onerror=onerror,
    ):
        current = Path(directory)
        try:
            resolved_current = current.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise Phase4Refusal(
                "untrusted-path",
                f"source directory cannot be resolved: {current}",
            ) from exc
        _require_below(resolved_current, root, "source directory")
        if current != root and _is_link_or_junction(current):
            raise Phase4Refusal(
                "untrusted-path",
                f"source directory is a link or junction: {current}",
            )

        relative = current.relative_to(root).as_posix()
        if relative != ".":
            folded = relative.casefold()
            previous = casefold_paths.get(folded)
            if previous is not None and previous != relative:
                raise Phase4Refusal(
                    "untrusted-path",
                    f"source directory case collision: {previous!r} and {relative!r}",
                )
            casefold_paths[folded] = relative
            directories.add(relative)

            boundary_files = [
                name for name in filenames if name.casefold() == "claude.md"
            ]
            if len(boundary_files) > 1:
                raise Phase4Refusal(
                    "untrusted-path",
                    f"source boundary filename case collision in {relative}: {boundary_files}",
                )
            if boundary_files:
                boundaries.add(relative)
                dirnames[:] = []
                continue

        safe_children: list[str] = []
        for dirname in sorted(dirnames):
            child = current / dirname
            if _should_prune_source(dirname):
                continue
            try:
                resolved_child = child.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise Phase4Refusal(
                    "untrusted-path",
                    f"source child directory cannot be resolved: {child}",
                ) from exc
            if resolved_child == docs_root:
                continue
            if _is_link_or_junction(child):
                raise Phase4Refusal(
                    "untrusted-path",
                    f"source child directory is a link or junction: {child}",
                )
            _require_below(resolved_child, root, "source child directory")
            identity = os.path.normcase(os.fspath(resolved_child))
            if identity in visited:
                raise Phase4Refusal(
                    "untrusted-path",
                    f"source directory identity is repeated: {child}",
                )
            visited.add(identity)
            safe_children.append(dirname)
        dirnames[:] = safe_children

    if walk_errors:
        raise Phase4Refusal(
            "source-inventory-incomplete",
            f"cannot completely walk source directories: {walk_errors[0]}",
        )
    return SourceInventory(directories=directories, boundaries=boundaries)


def _boundary_ancestor(source_relative: str, boundaries: set[str]) -> str | None:
    source_parts = tuple(
        part.casefold() for part in PurePosixPath(source_relative).parts
    )
    for boundary in sorted(boundaries):
        boundary_parts = tuple(
            part.casefold() for part in PurePosixPath(boundary).parts
        )
        if (
            len(source_parts) > len(boundary_parts)
            and source_parts[: len(boundary_parts)] == boundary_parts
        ):
            return boundary
    return None


def _unknown_scope_detail(
    source_relative: str,
    scopes: tuple[dict[str, str], ...],
) -> str | None:
    source_parts = tuple(
        part.casefold() for part in PurePosixPath(source_relative).parts
    )
    for scope in sorted(
        scopes,
        key=lambda item: (
            str(item.get("path", "")).casefold(),
            str(item.get("scope", "")),
        ),
    ):
        path = scope.get("path")
        scope_kind = scope.get("scope")
        reason = scope.get("reason")
        if not isinstance(path, str) or not isinstance(scope_kind, str):
            continue
        scope_parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
        same_or_below = source_parts[: len(scope_parts)] == scope_parts
        applies = same_or_below and (
            scope_kind == "subtree"
            or (scope_kind == "descendants" and len(source_parts) > len(scope_parts))
        )
        if applies:
            return f"{path} ({reason or 'unknown'}; {scope_kind})"
    return None


def _source_path_state(
    root: Path,
    source_relative: str,
    inventory: SourceInventory,
) -> tuple[str, str | None]:
    """Return ``present``, ``missing``, ``boundary``, or ``untrusted``."""

    boundary = _boundary_ancestor(source_relative, inventory.boundaries)
    if boundary is not None:
        return "boundary", boundary

    unknown = _unknown_scope_detail(source_relative, inventory.unknown_scopes)
    if unknown is not None:
        return "untrusted", f"source path is inside an unknown scan scope: {unknown}"

    folded_inventory = {path.casefold(): path for path in inventory.directories}
    if source_relative.casefold() in folded_inventory:
        return "present", folded_inventory[source_relative.casefold()]
    if inventory.frozen:
        return "missing", None

    pure_source = PurePosixPath(source_relative)
    lexical = root.joinpath(*pure_source.parts)
    try:
        _validate_existing_components(root, lexical, "corresponding source path")
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, Phase4Refusal) as exc:
        return "untrusted", str(exc)
    try:
        _require_below(resolved, root, "corresponding source path")
    except Phase4Refusal as exc:
        return "untrusted", exc.message

    if _lexists(lexical):
        try:
            if lexical.is_dir():
                return "present", source_relative
        except OSError as exc:
            return "untrusted", str(exc)
        return "untrusted", "corresponding source path exists but is not a directory"
    return "missing", None


def _has_exact_auto_marker(content: str) -> bool:
    return AUTO_MARKER in content.splitlines()


def _build_inbound_sources(
    active_paths: set[str],
    docs_root: Path,
) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
    inbound: dict[str, set[str]] = {path: set() for path in active_paths}
    invalid_links: list[dict[str, str]] = []
    for source_relative in sorted(active_paths):
        try:
            safe_source = contract.resolve_manifest_document(
                docs_root,
                source_relative,
                must_exist=True,
            )
            links = contract.read_markdown_links(safe_source.resolved_path, docs_root)
        except contract.ProjectDocsContractError as exc:
            raise Phase4Refusal(
                "active-document-invalid",
                f"active document cannot be read safely: {source_relative}: {exc}",
            ) from exc
        except OSError as exc:
            raise Phase4Refusal(
                "active-document-unreadable",
                f"active document cannot be read: {source_relative}: {exc}",
            ) from exc
        for link in links:
            if link.disposition is contract.LinkDisposition.INVALID:
                invalid_links.append(
                    {
                        "source": source_relative,
                        "target": link.raw_target,
                        "reason": link.reason or "invalid-local-target",
                    }
                )
                continue
            if not link.is_local_document or link.project_relative_path is None:
                continue
            target = link.project_relative_path
            if _is_quarantine_relative(target) or target == source_relative:
                continue
            inbound.setdefault(target, set()).add(source_relative)
    return inbound, invalid_links


def _load_manifest(docs_root: Path) -> tuple[dict[str, Any], list[str], str]:
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    if not _lexists(manifest_path):
        raise Phase4Refusal(
            "manifest-missing",
            f"required manifest is missing: {manifest_path}",
        )
    if _is_link_or_junction(manifest_path):
        raise Phase4Refusal(
            "untrusted-path",
            f"manifest must not be a link: {manifest_path}",
        )
    captured: list[str] = []
    try:
        raw_manifest = manifest_path.read_bytes()
        try:
            manifest_text = raw_manifest.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise contract.StrictUtf8Error(
                f"invalid UTF-8 in {manifest_path}: {exc}"
            ) from exc
        raw_value = contract.parse_json_strict(
            manifest_text,
            source=str(manifest_path),
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            manifest = contract.validate_manifest(
                raw_value,
                docs_root,
                verify_files=True,
                verify_complete_set=False,
            )
        captured.extend(str(item.message) for item in caught)
        manifest_digest = contract.sha256_bytes(raw_manifest)
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("manifest-invalid", str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "manifest-unreadable",
            f"manifest cannot be read safely: {exc}",
        ) from exc
    return manifest, captured, manifest_digest


def _analyze_loaded_snapshot(
    root: Path,
    docs_root: Path,
    manifest: dict[str, Any],
    manifest_warnings: list[str],
    manifest_sha256: str,
    *,
    source_inventory: SourceInventory | None = None,
) -> Analysis:
    """Analyze one already validated document/manifest snapshot."""

    manifest_entries: dict[str, dict[str, Any]] = {}
    for raw_entry in manifest["files"]:
        path = raw_entry["path"]
        if _is_quarantine_relative(path):
            raise Phase4Refusal(
                "manifest-registers-quarantine",
                f"manifest must not register quarantined content: {path}",
            )
        manifest_entries[path] = dict(raw_entry)

    active_paths = _discover_active_documents(docs_root)
    manifest_paths = set(manifest_entries)
    quarantine_root = _validate_existing_quarantine(docs_root)
    if quarantine_root is not None:
        _require_no_archive_active_alias(
            docs_root,
            quarantine_root,
            active_paths & manifest_paths,
        )
    casefold_active = {path.casefold(): path for path in active_paths}
    casefold_manifest = {path.casefold(): path for path in manifest_paths}
    mismatched_case = sorted(
        active_path
        for folded, active_path in casefold_active.items()
        if folded in casefold_manifest and casefold_manifest[folded] != active_path
    )
    unregistered = sorted(active_paths - manifest_paths)
    missing_registered = sorted(manifest_paths - active_paths)

    if source_inventory is None:
        source_inventory = _discover_source_directories(root, docs_root)
    elif not isinstance(source_inventory, SourceInventory):
        raise Phase4Refusal(
            "source-inventory-invalid",
            "source inventory override must be a SourceInventory",
        )
    inbound_sources, invalid_links = _build_inbound_sources(active_paths, docs_root)

    generation_value = manifest.get("generation_id")
    generation = (
        generation_value
        if isinstance(generation_value, str) and _SHA256_RE.fullmatch(generation_value)
        else contract.compute_generation_id(manifest["files"])
    )

    analysis = Analysis(
        root=root,
        docs_root=docs_root,
        manifest=manifest,
        manifest_entries=manifest_entries,
        generation=generation,
        manifest_sha256=manifest_sha256,
        active_paths=active_paths,
        source_inventory=source_inventory,
        inbound_sources=inbound_sources,
        warnings=manifest_warnings,
        unregistered=unregistered,
    )

    if mismatched_case:
        analysis.errors.append(
            {
                "code": "document-path-case-mismatch",
                "message": "manifest and active document path case differs: "
                + ", ".join(mismatched_case),
            }
        )
    if unregistered:
        analysis.errors.append(
            {
                "code": "unregistered-documents",
                "message": "active documents are absent from the manifest: "
                + ", ".join(unregistered),
            }
        )
    if missing_registered:
        analysis.errors.append(
            {
                "code": "manifest-documents-missing",
                "message": "manifest documents are absent from the active inventory: "
                + ", ".join(missing_registered),
            }
        )
    for invalid in invalid_links:
        analysis.errors.append(
            {
                "code": "invalid-markdown-target",
                "message": (
                    f"{invalid['source']} contains unsafe target {invalid['target']!r} "
                    f"({invalid['reason']})"
                ),
            }
        )

    active_casefold = {path.casefold() for path in active_paths}
    analysis.missing_docs = sorted(
        (f"{source}.md" if source else "index.md")
        for source in source_inventory.directories
        if (f"{source}.md" if source else "index.md").casefold() not in active_casefold
    )

    for path in sorted(manifest_paths):
        source_relative = "" if path.casefold() == "index.md" else path[:-3]
        source_state, source_detail = _source_path_state(
            root,
            source_relative,
            source_inventory,
        )
        if source_state == "present":
            continue

        inbound = sorted(inbound_sources.get(path, set()))
        item: dict[str, Any] = {
            "path": path,
            "source_path": source_relative,
            "inbound_references": len(inbound),
            "inbound_sources": inbound,
        }
        if source_state == "boundary":
            item.update(
                {
                    "reason": "boundary-descendant",
                    "boundary": source_detail,
                }
            )
            analysis.blocked.append(item)
            continue
        if source_state == "untrusted":
            item.update(
                {
                    "reason": "untrusted-source-path",
                    "detail": source_detail,
                }
            )
            analysis.blocked.append(item)
            analysis.errors.append(
                {
                    "code": "untrusted-source-path",
                    "message": f"cannot prove source absence for {path}: {source_detail}",
                }
            )
            continue

        safe_doc = contract.resolve_manifest_document(docs_root, path, must_exist=True)
        content = contract.read_utf8(safe_doc.resolved_path)
        if not _has_exact_auto_marker(content):
            item["reason"] = "missing-exact-auto-marker"
            analysis.blocked.append(item)
            continue
        if inbound:
            item["reason"] = "inbound-references"
            analysis.blocked.append(item)
            continue

        item.update(
            {
                "reason": QUARANTINE_REASON,
                "sha256": manifest_entries[path]["sha256"],
                "generation": generation,
            }
        )
        analysis.candidates.append(item)

    return analysis


def analyze_project(root_value: str | os.PathLike[str]) -> Analysis:
    """Build a strict report snapshot without mutating the filesystem."""

    root, docs_root = _resolve_project(root_value)
    manifest, manifest_warnings, manifest_sha256 = _load_manifest(docs_root)
    return _analyze_loaded_snapshot(
        root,
        docs_root,
        manifest,
        manifest_warnings,
        manifest_sha256,
    )


def analyze_snapshot(
    root_value: str | os.PathLike[str],
    docs_root_value: str | os.PathLike[str],
    manifest_value: dict[str, Any],
    *,
    source_inventory: SourceInventory | None = None,
) -> Analysis:
    """Analyze a staged v2 snapshot against one trusted source inventory.

    This entry point is report-only.  It deliberately accepts no apply action
    and therefore cannot quarantine files from a staging directory.  A caller
    may bind the report to an already validated frozen scanner inventory.
    """

    supplied_root = Path(root_value)
    supplied_docs = Path(docs_root_value)
    try:
        root = supplied_root.resolve(strict=True)
        docs_root = supplied_docs.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "snapshot-path-invalid",
            f"staged snapshot cannot be resolved: {exc}",
        ) from exc
    if not root.is_dir() or not docs_root.is_dir():
        raise Phase4Refusal(
            "snapshot-path-invalid",
            "project root and staged project_docs must both be directories",
        )
    if _is_link_or_junction(docs_root):
        raise Phase4Refusal(
            "untrusted-path",
            f"staged project_docs must not be a link or junction: {docs_root}",
        )
    _require_below(docs_root, root, "staged project_docs")
    _validate_existing_components(root, docs_root, "staged project_docs")
    _validate_existing_quarantine(docs_root)

    manifest = dict(manifest_value)
    try:
        contract.validate_manifest_v2(manifest, docs_root, verify_files=True)
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("manifest-invalid", str(exc)) from exc
    manifest_sha256 = contract.sha256_bytes(
        contract.stable_json_dumps(manifest).encode("utf-8")
    )
    return _analyze_loaded_snapshot(
        root,
        docs_root,
        manifest,
        [],
        manifest_sha256,
        source_inventory=source_inventory,
    )


def _analysis_result(analysis: Analysis, action: str, mode: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "ok": analysis.safe,
        "action": action,
        "mode": mode,
        "counts": {
            "source_directories": len(analysis.source_inventory.directories),
            "active_documents": len(analysis.active_paths),
            "manifest_documents": len(analysis.manifest_entries),
            "candidates": len(analysis.candidates),
            "blocked": len(analysis.blocked),
            "missing_docs": len(analysis.missing_docs),
            "unregistered": len(analysis.unregistered),
        },
        "generation": analysis.generation,
        "candidates": analysis.candidates,
        "blocked": analysis.blocked,
        "missing_docs": analysis.missing_docs,
        "unregistered": analysis.unregistered,
        "quarantined": [],
        "restored": None,
        "warnings": list(analysis.warnings),
        "errors": list(analysis.errors),
    }
    return result


def analysis_result(analysis: Analysis, action: str, mode: str) -> dict[str, Any]:
    """Render a stable public report for an existing analysis snapshot."""

    return _analysis_result(analysis, action, mode)


def _empty_result(action: str, mode: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": False,
        "action": action,
        "mode": mode,
        "counts": {},
        "generation": None,
        "candidates": [],
        "blocked": [],
        "missing_docs": [],
        "unregistered": [],
        "quarantined": [],
        "restored": None,
        "warnings": [],
        "errors": [],
    }


def _receipt_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:16]}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _trusted_source_missing(analysis: Analysis, path: str) -> None:
    current_inventory = _discover_source_directories(analysis.root, analysis.docs_root)
    source_relative = "" if path.casefold() == "index.md" else path[:-3]
    state, detail = _source_path_state(analysis.root, source_relative, current_inventory)
    if state != "missing":
        raise Phase4Refusal(
            "source-no-longer-confirmed-missing",
            f"source absence is no longer established for {path}: {state} ({detail})",
        )


def _revalidate_candidate(
    analysis: Analysis,
    candidate: dict[str, Any],
    expected_active: set[str],
) -> Path:
    path = candidate["path"]
    if path not in analysis.manifest_entries:
        raise Phase4Refusal(
            "candidate-unregistered",
            f"candidate is no longer registered in the manifest snapshot: {path}",
        )
    manifest_path = analysis.docs_root / contract.MANIFEST_FILENAME
    if contract.sha256_file(manifest_path) != analysis.manifest_sha256:
        raise Phase4Refusal(
            "manifest-changed",
            "manifest changed after Phase 4 analysis",
        )

    current_active = _discover_active_documents(analysis.docs_root)
    if current_active != expected_active:
        added = sorted(current_active - expected_active)
        removed = sorted(expected_active - current_active)
        raise Phase4Refusal(
            "active-inventory-changed",
            f"active document inventory changed (added={added}, removed={removed})",
        )
    if path not in current_active:
        raise Phase4Refusal("candidate-missing", f"candidate disappeared: {path}")

    safe_path = contract.resolve_manifest_document(
        analysis.docs_root,
        path,
        must_exist=True,
    )
    content = contract.read_utf8(safe_path.resolved_path)
    if not _has_exact_auto_marker(content):
        raise Phase4Refusal(
            "candidate-marker-changed",
            f"candidate no longer has the exact ownership marker: {path}",
        )
    current_digest = contract.sha256_file(safe_path.resolved_path)
    expected_digest = analysis.manifest_entries[path]["sha256"]
    if current_digest != expected_digest:
        raise Phase4Refusal(
            "candidate-hash-changed",
            f"candidate hash differs from the manifest: {path}",
        )

    _trusted_source_missing(analysis, path)
    inbound, invalid_links = _build_inbound_sources(current_active, analysis.docs_root)
    if invalid_links:
        first = invalid_links[0]
        raise Phase4Refusal(
            "invalid-markdown-target",
            f"unsafe Markdown target appeared in {first['source']}: {first['target']!r}",
        )
    referrers = sorted(inbound.get(path, set()))
    if referrers:
        raise Phase4Refusal(
            "candidate-has-inbound-references",
            f"candidate gained inbound references from: {', '.join(referrers)}",
        )
    return safe_path.resolved_path


def _new_transaction_paths(quarantine_root: Path) -> tuple[str, Path, Path]:
    for _ in range(20):
        receipt_id = _receipt_id()
        if _RECEIPT_ID_RE.fullmatch(receipt_id) is None:
            raise Phase4OperationalError(
                "receipt-id-generation-failed",
                f"generated receipt ID is invalid: {receipt_id!r}",
            )
        final_dir = quarantine_root / receipt_id
        staging = quarantine_root / f".staging-{receipt_id}-{uuid.uuid4().hex[:8]}"
        if not _lexists(final_dir) and not _lexists(staging):
            return receipt_id, staging, final_dir
    raise Phase4OperationalError(
        "receipt-id-collision",
        "could not allocate a unique receipt ID",
    )


def _identity_from_stat(info: os.stat_result) -> tuple[int, int, int, int]:
    # On Windows, st_file_attributes can change spuriously (e.g. cloud-sync
    # filter drivers toggling FILE_ATTRIBUTE_OFFLINE/NOT_CONTENT_INDEXED).
    # Use 0 as a stable placeholder to avoid false TOCTOU failures.
    return (
        int(getattr(info, "st_dev", 0)),
        int(getattr(info, "st_ino", 0)),
        int(stat.S_IFMT(info.st_mode)),
        0,
    )


def _trusted_directory_identity(
    path: Path, description: str
) -> tuple[int, int, int, int]:
    """Return a stable directory identity or refuse an indirect/raced path."""

    if not _lexists(path) or _is_link_or_junction(path) or not path.is_dir():
        raise Phase4Refusal(
            "quarantine-rename-conflict",
            f"{description} is missing, indirect, or not a directory: {path}",
        )
    try:
        return _identity_from_stat(path.lstat())
    except OSError as exc:
        raise Phase4Refusal(
            "quarantine-rename-conflict",
            f"cannot inspect {description}: {path}: {exc}",
        ) from exc


def _rename_staged_quarantine_directory(
    staging_dir: Path,
    final_dir: Path,
    *,
    expected_files: Mapping[str, Mapping[str, Any]],
    is_windows: bool | None = None,
    sleep_func: Any = time.sleep,
    max_attempts: int = _WINDOWS_RENAME_MAX_ATTEMPTS,
) -> None:
    """Rename a closed staging directory, retrying only transient Windows locks.

    每次重试前后都复核目录身份、payload、receipt 和封闭成员集合。
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    windows = os.name == "nt" if is_windows is None else is_windows
    expected_identity = _require_closed_transaction_directory(
        staging_dir,
        staging_dir.parent,
        expected_files,
    )
    if _lexists(final_dir):
        raise Phase4Refusal(
            "quarantine-rename-conflict",
            f"quarantine final directory already exists: {final_dir}",
        )
    for attempt in range(max_attempts):
        if _lexists(final_dir):
            raise Phase4Refusal(
                "quarantine-rename-conflict",
                f"quarantine final directory appeared during rename: {final_dir}",
            )
        current_identity = _require_closed_transaction_directory(
            staging_dir,
            staging_dir.parent,
            expected_files,
        )
        if current_identity != expected_identity:
            raise Phase4Refusal(
                "quarantine-rename-conflict",
                f"quarantine staging directory identity changed: {staging_dir}",
            )
        try:
            _rename_no_replace(staging_dir, final_dir)
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            transient = windows and winerror in _WINDOWS_RENAME_RETRY_WINERRORS
            if not transient:
                raise
            if _lexists(final_dir):
                raise Phase4Refusal(
                    "quarantine-rename-conflict",
                    f"quarantine final directory appeared during retry: {final_dir}",
                ) from exc
            current_identity = _require_closed_transaction_directory(
                staging_dir,
                staging_dir.parent,
                expected_files,
            )
            if current_identity != expected_identity:
                raise Phase4Refusal(
                    "quarantine-rename-conflict",
                    f"quarantine staging directory identity changed during retry: {staging_dir}",
                ) from exc
            if attempt + 1 >= max_attempts:
                raise Phase4OperationalError(
                    "quarantine-rename-retry-exhausted",
                    f"Windows quarantine directory rename remained locked after {max_attempts} attempts: {exc}",
                ) from exc
            sleep_func(_WINDOWS_RENAME_INITIAL_DELAY_SECONDS * (2**attempt))
            continue
        if _lexists(staging_dir):
            raise Phase4Refusal(
                "quarantine-rename-conflict",
                f"quarantine staging directory remained after rename: {staging_dir}",
            )
        final_identity = _require_closed_transaction_directory(
            final_dir,
            final_dir.parent,
            expected_files,
        )
        if final_identity != expected_identity:
            raise Phase4Refusal(
                "quarantine-rename-conflict",
                f"quarantine final directory identity differs after rename: {final_dir}",
            )
        _fsync_directory(final_dir.parent)
        return


def _exclusive_copy_file(
    source: Path,
    destination: Path,
    expected_digest: str,
    expected_identity: tuple[int, int, int, int],
    *,
    archive_root: Path,
    archive_trusted_root: Path,
    cleanup_on_failure: bool = True,
) -> ExclusiveCopy:
    """以 no-replace rename 将独立 staged inode 发布为 restore 目标。"""

    source_snapshot, source_identity = _capture_snapshot_with_identity(
        source,
        archive_trusted_root,
        "restore staged target",
    )
    if (
        source_identity != expected_identity
        or source_snapshot.get("sha256") != expected_digest
    ):
        raise OSError(f"restore staged target identity or hash mismatch: {source}")

    created: ExclusiveCopy | None = None
    try:
        _rename_no_replace(source, destination)
        # Establish identity-bound ownership immediately after rename.  Any later
        # pathname validation failure can then remove only this persisted inode.
        created = ExclusiveCopy(
            destination,
            expected_identity,
            archive_root,
            archive_trusted_root,
            source_snapshot,
        )
        destination_snapshot, destination_identity = _capture_snapshot_with_identity(
            destination,
            archive_trusted_root,
            "exclusive restore destination",
        )
        if (
            destination_identity != expected_identity
            or destination_snapshot != source_snapshot
        ):
            raise OSError(
                f"exclusive restore destination differs from staged target: {destination}"
            )
        if _lexists(source):
            raise OSError(f"restore staged target still exists after rename: {source}")
        _fsync_directory(destination.parent)
        if source.parent != destination.parent:
            _fsync_directory(source.parent)
        return created
    except BaseException as exc:
        cleanup_error: BaseException | None = None
        if cleanup_on_failure and created is not None and _lexists(destination):
            try:
                _unlink_exclusive_copy(created)
            except BaseException as remove_exc:  # pragma: no cover - catastrophic race
                cleanup_error = remove_exc
        if cleanup_error is not None:
            raise Phase4OperationalError(
                "exclusive-copy-cleanup-failed",
                f"exclusive publication failed ({exc}); cleanup also failed ({cleanup_error})",
            ) from exc
        raise


def _unlink_exclusive_copy(created: ExclusiveCopy) -> None:
    if not _lexists(created.path):
        return
    try:
        _detach_and_remove_regular_file(
            created.path,
            created.identity,
            "exclusive destination",
            expected_snapshot=created.snapshot,
            conflict_code="exclusive-copy-conflict",
            operational_code="exclusive-copy-cleanup-failed",
            archive_root=created.archive_root,
            archive_trusted_root=created.archive_trusted_root,
        )
    except (Phase4Refusal, Phase4OperationalError) as exc:
        raise OSError(exc.message) from exc


def _new_quarantine_move(
    analysis: Analysis,
    candidate: dict[str, Any],
    source_path: Path,
    quarantine_root: Path,
) -> QuarantineMove:
    receipt_id, staging, final_dir = _new_transaction_paths(quarantine_root)
    original = candidate["path"]
    payload_relative = f"{PAYLOAD_DIRNAME}/{original}"
    payload_parts = PurePosixPath(payload_relative).parts
    receipt = {
        "schema_version": 2,
        "receipt_id": receipt_id,
        "original_path": original,
        "quarantined_path": payload_relative,
        "payload_identity": None,
        "sha256": candidate["sha256"],
        "reason": QUARANTINE_REASON,
        "generation": analysis.generation,
        "created_at": _utc_now(),
        "state": "quarantined",
        "restored_at": None,
    }
    return QuarantineMove(
        receipt=receipt,
        source_path=source_path,
        staging_dir=staging,
        final_dir=final_dir,
        staging_payload=staging.joinpath(*payload_parts),
        final_payload=final_dir.joinpath(*payload_parts),
    )


def _link_source_to_staging_no_replace(move: QuarantineMove) -> None:
    """Atomically move one exact source inode into quarantine staging."""

    original = move.receipt["original_path"]
    source = move.source_path
    destination = move.staging_payload
    docs_root = move.staging_dir.parent.parent
    if _lexists(destination):
        raise Phase4Refusal(
            "quarantine-staging-conflict",
            f"quarantine staging payload already exists: {destination}",
        )
    source_snapshot, source_identity = _capture_snapshot_with_identity(
        source,
        docs_root,
        f"quarantine source {original}",
    )
    if not source_snapshot["exists"] or source_identity is None:
        raise Phase4Refusal(
            "candidate-path-changed",
            f"candidate disappeared before quarantine: {original}",
        )
    persisted_identity = _validate_serialized_identity(
        move.receipt.get("payload_identity"),
        "receipt payload_identity",
    )
    if source_identity != persisted_identity:
        raise Phase4Refusal(
            "candidate-path-changed",
            f"candidate identity changed before quarantine: {original}",
        )
    if source_snapshot["sha256"] != move.receipt["sha256"]:
        raise Phase4Refusal(
            "candidate-hash-changed",
            f"candidate changed before quarantine: {original}",
        )
    if not _has_exact_auto_marker(source_snapshot["text"]):
        raise Phase4Refusal(
            "candidate-marker-changed",
            f"candidate marker changed before quarantine: {original}",
        )

    moved = False
    try:
        _transaction_fault("quarantine-before-source-link")
        try:
            current, current_identity = _capture_snapshot_with_identity(
                source,
                docs_root,
                f"quarantine source {original}",
            )
        except Phase4Refusal as capture_exc:
            raise Phase4Refusal(
                "candidate-path-changed",
                f"candidate became untrusted before quarantine: {original}",
            ) from capture_exc
        if current != source_snapshot or current_identity != source_identity:
            raise Phase4Refusal(
                "candidate-path-changed",
                f"candidate changed before moving into quarantine: {original}",
            )
        try:
            _rename_no_replace(source, destination)
        except FileExistsError as exc:
            raise Phase4Refusal(
                "quarantine-staging-conflict",
                f"quarantine staging payload appeared concurrently: {destination}",
            ) from exc
        moved = True
        destination_snapshot, destination_identity = _capture_snapshot_with_identity(
            destination,
            move.staging_dir,
            f"quarantine staging payload {original}",
        )
        if (
            destination_snapshot != source_snapshot
            or destination_identity != source_identity
        ):
            raise Phase4Refusal(
                "candidate-path-changed",
                f"candidate changed while moving into quarantine: {original}",
            )
        move.moved = True
        _fsync_directory(source.parent)
        if destination.parent != source.parent:
            _fsync_directory(destination.parent)
    except BaseException as exc:
        if moved and _lexists(destination):
            try:
                destination_now, destination_identity = _capture_snapshot_with_identity(
                    destination,
                    move.staging_dir,
                    f"quarantine staging payload {original}",
                )
                if (
                    destination_now == source_snapshot
                    and destination_identity == source_identity
                    and not _lexists(source)
                ):
                    _rename_no_replace(destination, source)
                    restored, restored_identity = _capture_snapshot_with_identity(
                        source,
                        docs_root,
                        f"restored quarantine source {original}",
                    )
                    if (
                        restored != source_snapshot
                        or restored_identity != source_identity
                    ):
                        raise OSError("restored source differs from its preimage")
                    _fsync_directory(source.parent)
                    if destination.parent != source.parent:
                        _fsync_directory(destination.parent)
                    move.moved = False
                else:
                    raise OSError("source or staging path changed before move rollback")
            except BaseException as rollback_exc:
                raise Phase4OperationalError(
                    "quarantine-move-rollback-failed",
                    f"quarantine move failed ({exc}); cannot restore its exact source: "
                    f"{rollback_exc}",
                ) from exc
        raise


def _execute_quarantine_move(analysis: Analysis, move: QuarantineMove) -> None:
    original = move.receipt["original_path"]
    _ensure_directory_chain(
        move.staging_dir.parent,
        move.staging_dir,
        "quarantine staging directory",
    )
    _ensure_directory_chain(
        move.staging_dir,
        move.staging_payload.parent,
        "quarantine payload parent",
    )

    # 全量预校验之后，仍在最终处置相邻位置重复最关键的不可变条件。
    if contract.sha256_file(move.source_path) != move.receipt["sha256"]:
        raise Phase4Refusal(
            "candidate-hash-changed",
            f"candidate changed before quarantine: {original}",
        )
    if not _has_exact_auto_marker(contract.read_utf8(move.source_path)):
        raise Phase4Refusal(
            "candidate-marker-changed",
            f"candidate marker changed before quarantine: {original}",
        )
    _trusted_source_missing(analysis, original)

    receipt_snapshot = _text_snapshot(contract.stable_json_dumps(move.receipt))
    _write_exclusive_snapshot(
        move.staging_dir / RECEIPT_FILENAME,
        move.staging_dir,
        f"quarantine receipt {move.receipt['receipt_id']}",
        receipt_snapshot,
        staging_root=move.staging_dir.parent,
    )
    _link_source_to_staging_no_replace(move)
    payload_snapshot = _capture_snapshot(
        move.staging_payload,
        move.staging_dir,
        f"quarantine payload {move.receipt['receipt_id']}",
    )
    if payload_snapshot["sha256"] != move.receipt["sha256"]:
        raise Phase4OperationalError(
            "quarantine-hash-mismatch",
            f"payload hash changed during quarantine: {original}",
        )
    expected_files = {
        RECEIPT_FILENAME: receipt_snapshot,
        f"{PAYLOAD_DIRNAME}/{original}": payload_snapshot,
    }
    _rename_staged_quarantine_directory(
        move.staging_dir,
        move.final_dir,
        expected_files=expected_files,
    )
    move.finalized = True


def _manifest_snapshot_text(analysis: Analysis) -> str:
    manifest_path = analysis.docs_root / contract.MANIFEST_FILENAME
    before = contract.read_utf8(manifest_path)
    if contract.sha256_bytes(before.encode("utf-8")) != analysis.manifest_sha256:
        raise Phase4Refusal(
            "manifest-changed",
            "manifest changed after the locked Phase 4 analysis",
        )
    return before


def _build_current_manifest(analysis: Analysis) -> dict[str, Any]:
    producer_value = analysis.manifest.get("producer")
    producer: Mapping[str, str] | None = None
    if isinstance(producer_value, Mapping):
        name = producer_value.get("name")
        version = producer_value.get("version")
        if isinstance(name, str) and name and isinstance(version, str) and version:
            producer = {"name": name, "version": version}
    label = analysis.manifest.get("docs_root")
    docs_root_label = (
        label if isinstance(label, str) and label else analysis.docs_root.name
    )
    return contract.build_manifest(
        analysis.docs_root,
        producer=producer,
        last_synced=_utc_now(),
        docs_root_label=docs_root_label,
    )


def _require_quarantine_commit_state(
    analysis: Analysis,
    moves: Sequence[QuarantineMove],
    manifest_before: str,
    *,
    expected_manifest: Mapping[str, Any] | None = None,
    manifest_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """在 metadata/manifest 发布相邻位置复核整个 quarantine 后状态。"""

    manifest_path = analysis.docs_root / contract.MANIFEST_FILENAME
    _require_snapshot_state(
        manifest_path,
        analysis.docs_root,
        "manifest",
        (
            _text_snapshot(manifest_before)
            if manifest_snapshot is None
            else manifest_snapshot
        ),
        conflict_code="manifest-changed",
    )
    removed = {move.receipt["original_path"] for move in moves}
    expected_active = analysis.active_paths - removed
    current_active = _discover_active_documents(analysis.docs_root)
    if current_active != expected_active:
        raise Phase4Refusal(
            "active-inventory-changed",
            "active document inventory changed before quarantine commit "
            f"(added={sorted(current_active - expected_active)}, "
            f"removed={sorted(expected_active - current_active)})",
        )
    inbound, invalid_links = _build_inbound_sources(current_active, analysis.docs_root)
    if invalid_links:
        first = invalid_links[0]
        raise Phase4Refusal(
            "invalid-markdown-target",
            f"unsafe Markdown target appeared in {first['source']}: {first['target']!r}",
        )
    for move in moves:
        original = move.receipt["original_path"]
        referrers = sorted(inbound.get(original, set()))
        if referrers:
            raise Phase4Refusal(
                "candidate-has-inbound-references",
                f"candidate gained inbound references from: {', '.join(referrers)}",
            )
        _trusted_source_missing(analysis, original)
        _source_is_absent(
            analysis.docs_root.joinpath(*PurePosixPath(original).parts),
            analysis.docs_root,
            f"quarantined active document {original}",
        )
        receipt_snapshot = _text_snapshot(contract.stable_json_dumps(move.receipt))
        payload_snapshot, payload_identity = _capture_snapshot_with_identity(
            move.final_payload,
            move.final_dir,
            f"quarantine payload {move.receipt['receipt_id']}",
        )
        expected_payload_identity = _receipt_payload_identity(
            move.receipt,
            error_code="quarantine-payload-identity-mismatch",
            context=f"quarantine receipt {move.receipt['receipt_id']}",
        )
        if payload_snapshot["sha256"] != move.receipt["sha256"]:
            raise Phase4Refusal(
                "quarantine-hash-mismatch",
                f"quarantine payload changed before commit: {original}",
            )
        if payload_identity != expected_payload_identity:
            raise Phase4Refusal(
                "quarantine-payload-identity-mismatch",
                f"quarantine payload identity changed before commit: {original}",
            )
        payload_relative = f"{PAYLOAD_DIRNAME}/{original}"
        file_identities: dict[str, tuple[int, int, int, int]] = {}
        _require_closed_transaction_directory(
            move.final_dir,
            move.final_dir.parent,
            {
                RECEIPT_FILENAME: receipt_snapshot,
                payload_relative: payload_snapshot,
            },
            file_identities=file_identities,
        )
        if file_identities.get(payload_relative) != expected_payload_identity:
            raise Phase4Refusal(
                "quarantine-payload-identity-mismatch",
                f"quarantine payload identity changed during commit validation: {original}",
            )
    if expected_manifest is None:
        return _build_current_manifest(analysis)
    try:
        return contract.validate_manifest(
            dict(expected_manifest),
            analysis.docs_root,
            verify_files=True,
            verify_complete_set=True,
        )
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal(
            "manifest-postimage-stale",
            f"candidate manifest no longer matches the active document tree: {exc}",
        ) from exc


def _prepare_claude_update(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    archive_root: Path | None = None,
) -> TextUpdate | None:
    claude_path = root / "CLAUDE.md"
    if not _lexists(claude_path):
        return None
    if _is_link_or_junction(claude_path) or not claude_path.is_file():
        raise Phase4Refusal(
            "claude-doc-index-invalid",
            f"root CLAUDE.md is not a trusted regular file: {claude_path}",
        )
    before = contract.read_utf8(claude_path)
    start = claude_doc_index.section_start(claude_doc_index.DOC_TREE_SECTION)
    end = claude_doc_index.section_end(claude_doc_index.DOC_TREE_SECTION)
    if start not in before and end not in before:
        return None
    try:
        after = claude_doc_index.replace_doc_tree_section(before, manifest)
    except (
        claude_doc_index.ClaudeIndexError,
        contract.ProjectDocsContractError,
    ) as exc:
        raise Phase4Refusal(
            "claude-doc-index-invalid",
            f"root CLAUDE.md doc-tree markers are not safely replaceable: {exc}",
        ) from exc
    if after == before:
        return None
    return TextUpdate(
        path=claude_path,
        before=before,
        after=after,
        archive_root=archive_root,
        archive_trusted_root=root if archive_root is not None else None,
    )


def _observe_text_update_state(update: TextUpdate) -> TextUpdateState:
    """仅在能读取精确 pre/postimage 时给出确定状态。"""

    try:
        current = contract.read_utf8(update.path)
    except BaseException:
        update.state = TextUpdateState.UNKNOWN
        return update.state
    if current == update.before:
        update.state = TextUpdateState.BEFORE
    elif current == update.after:
        update.state = TextUpdateState.AFTER
    else:
        update.state = TextUpdateState.UNKNOWN
    return update.state


def _apply_text_update(update: TextUpdate) -> None:
    update.state = TextUpdateState.NOT_ATTEMPTED
    try:
        _set_snapshot_state(
            update.path,
            update.path.parent,
            "transaction metadata",
            _text_snapshot(update.after),
            [_text_snapshot(update.before)],
            archive_root=update.archive_root,
            archive_trusted_root=update.archive_trusted_root,
        )
        if _observe_text_update_state(update) is not TextUpdateState.AFTER:
            raise OSError(f"text publication verification failed: {update.path}")
        _fsync_directory(update.path.parent)
    except BaseException:
        _observe_text_update_state(update)
        raise


def _apply_json_update(update: TextUpdate, value: Mapping[str, Any]) -> None:
    rendered = contract.stable_json_dumps(dict(value))
    if rendered != update.after:
        raise Phase4Refusal(
            "recovery-conflict",
            f"JSON update does not match its prepared postimage: {update.path}",
        )
    _apply_text_update(update)


def _rollback_text_update(update: TextUpdate | None) -> list[str]:
    if update is None or update.state is TextUpdateState.NOT_ATTEMPTED:
        return []
    try:
        state = _observe_text_update_state(update)
        if state is TextUpdateState.BEFORE:
            return []
        if state is not TextUpdateState.AFTER:
            raise OSError(
                f"text target state is unknown during rollback: {update.path}"
            )
        try:
            _set_snapshot_state(
                update.path,
                update.path.parent,
                "transaction metadata rollback",
                _text_snapshot(update.before),
                [_text_snapshot(update.after)],
                archive_root=update.archive_root,
                archive_trusted_root=update.archive_trusted_root,
            )
        except BaseException:
            if _observe_text_update_state(update) is TextUpdateState.BEFORE:
                return []
            raise
        if _observe_text_update_state(update) is not TextUpdateState.BEFORE:
            raise OSError(f"text rollback verification failed: {update.path}")
        _fsync_directory(update.path.parent)
        return []
    except BaseException as exc:  # pragma: no cover - catastrophic path
        update.state = TextUpdateState.UNKNOWN
        return [f"rollback {update.path}: {exc}"]


def _restore_manifest_before(manifest_path: Path, before: str) -> list[str]:
    """Confirm rollback state without overwriting an unknown concurrent manifest."""

    try:
        current = contract.read_utf8(manifest_path)
        if current == before:
            return []
        raise OSError(
            "manifest differs from the transaction preimage; refusing to overwrite it"
        )
    except BaseException as exc:  # pragma: no cover - catastrophic path
        return [f"rollback {manifest_path}: {exc}"]


def _commit_manifest(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    journal: TransactionJournal,
    *,
    precommit_validator: Callable[[], None] | None = None,
    postcommit_validator: Callable[[], None] | None = None,
) -> ManifestCommitDisposition:
    """Commit through a detach/no-replace CAS while preserving both generations."""

    if journal.data["phase"] != "ready":
        raise Phase4Refusal(
            "recovery-journal-conflict",
            "manifest commit requires an immutable ready journal",
        )
    docs_root = manifest_path.parent
    before_snapshot = journal.data["manifest"]["before"]
    after_snapshot = journal.data["manifest"]["after"]
    rendered = contract.stable_json_dumps(dict(manifest))
    if after_snapshot != _text_snapshot(rendered):
        raise Phase4Refusal(
            "manifest-postimage-stale",
            "manifest candidate differs from the ready transaction journal",
        )
    before_artifact, after_artifact = _manifest_transaction_paths(
        docs_root,
        journal.data["transaction_id"],
    )
    if _lexists(before_artifact) or _lexists(after_artifact):
        raise Phase4Refusal(
            "manifest-changed",
            "manifest transaction artifacts already exist before commit",
        )

    _write_exclusive_snapshot(
        before_artifact,
        docs_root,
        "manifest transaction before anchor",
        before_snapshot,
        staging_root=journal.quarantine_root,
        staging_trusted_root=docs_root,
    )
    _transaction_fault("manifest-before-anchor-staged")
    _write_exclusive_snapshot(
        after_artifact,
        docs_root,
        "manifest transaction after candidate",
        after_snapshot,
        staging_root=journal.quarantine_root,
        staging_trusted_root=docs_root,
    )
    _transaction_fault("manifest-candidate-staged")
    if precommit_validator is not None:
        precommit_validator()
    current_manifest, expected_identity = _capture_snapshot_with_identity(
        manifest_path,
        docs_root,
        "manifest",
    )
    if current_manifest != before_snapshot or expected_identity is None:
        raise Phase4Refusal(
            "manifest-changed",
            "manifest differs from the ready transaction preimage",
        )

    try:
        _remove_snapshot_file(
            manifest_path,
            docs_root,
            "manifest",
            before_snapshot,
            expected_identity=expected_identity,
            archive_root=journal.quarantine_root,
            archive_trusted_root=docs_root,
        )
        # The durable before anchor must precede the durable canonical gap.  A
        # crash from this point can always reconstruct the manifest preimage.
        _fsync_directory(docs_root)
        _transaction_fault("manifest-before-detached")
        after_current, candidate_identity = _capture_snapshot_with_identity(
            after_artifact,
            docs_root,
            "manifest transaction after candidate",
        )
        if after_current != after_snapshot or candidate_identity is None:
            raise Phase4Refusal(
                "manifest-changed",
                "manifest transaction candidate changed before installation",
            )
        try:
            _rename_no_replace(after_artifact, manifest_path)
        except FileExistsError as exc:
            raise Phase4Refusal(
                "manifest-changed",
                "a concurrent manifest appeared in the commit gap; it was preserved",
            ) from exc
        if (
            _is_link_or_junction(manifest_path)
            or _identity_from_stat(manifest_path.lstat()) != candidate_identity
        ):
            raise Phase4Refusal(
                "manifest-changed",
                "published manifest identity differs from its transaction candidate",
            )
        _require_snapshot_state(
            manifest_path,
            docs_root,
            "manifest",
            after_snapshot,
            conflict_code="manifest-changed",
        )
        _fsync_directory(docs_root)
        _transaction_fault("manifest-candidate-installed")
    except Exception as exc:
        try:
            candidate_visible = _snapshot_matches(
                manifest_path,
                docs_root,
                "manifest",
                after_snapshot,
            )
        except BaseException:
            candidate_visible = False
        if candidate_visible:
            disposition = ManifestCommitDisposition.INSTALLED_DURABILITY_UNKNOWN
            raise Phase4OperationalError(
                "manifest-durability-unknown",
                "manifest candidate is visible, but its directory entry durability "
                f"was not confirmed ({disposition.value}): {exc}",
            ) from exc
        if not _lexists(manifest_path):
            try:
                _rename_no_replace(before_artifact, manifest_path)
                _require_snapshot_state(
                    manifest_path,
                    docs_root,
                    "restored manifest preimage",
                    before_snapshot,
                    conflict_code="manifest-changed",
                )
                _fsync_directory(docs_root)
            except FileExistsError:
                pass
            except BaseException as restore_exc:
                raise Phase4OperationalError(
                    "manifest-rollback-failed",
                    f"manifest CAS failed ({exc}); preimage restoration also failed: {restore_exc}",
                ) from exc
        raise
    if postcommit_validator is not None:
        try:
            postcommit_validator()
        except (Phase4Refusal, Phase4OperationalError) as exc:
            raise Phase4OperationalError(
                "manifest-postcommit-conflict",
                "manifest commit is durable, but a transaction participant changed "
                f"before postcommit validation completed: {exc.message}",
            ) from exc
        except Exception as exc:
            raise Phase4OperationalError(
                "manifest-postcommit-conflict",
                "manifest commit is durable, but postcommit validation failed: "
                f"{exc}",
            ) from exc
    return ManifestCommitDisposition.DURABLE


def _batch_quarantine(
    analysis: Analysis,
    validated: Sequence[tuple[dict[str, Any], Path]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """Run one write-ahead logged quarantine batch under the caller's root lock."""

    quarantine_root = _prepare_quarantine_root(analysis.docs_root)
    manifest_path = analysis.docs_root / contract.MANIFEST_FILENAME
    manifest_before = _manifest_snapshot_text(analysis)
    claude_before = _capture_snapshot(
        analysis.root / "CLAUDE.md", analysis.root, "CLAUDE.md"
    )
    moves = [
        _new_quarantine_move(analysis, candidate, source_path, quarantine_root)
        for candidate, source_path in validated
    ]
    claude_update: TextUpdate | None = None
    journal: TransactionJournal | None = None
    warnings_out: list[str] = []

    try:
        # This is deliberately the first transaction mutation.  A freshly created
        # quarantine root is only a container; no source or metadata changes occur
        # until its durable journal has been read back and validated.
        journal = _write_transaction_journal(
            quarantine_root,
            _quarantine_journal_prepared(
                analysis,
                moves,
                manifest_before,
                claude_before,
            ),
        )
        _transaction_fault("quarantine-journal-created")
        for move in moves:
            _execute_quarantine_move(analysis, move)
        _transaction_fault("quarantine-moves-complete")

        next_manifest = _require_quarantine_commit_state(
            analysis,
            moves,
            manifest_before,
        )
        claude_update = _prepare_claude_update(
            analysis.root,
            next_manifest,
            archive_root=analysis.docs_root / QUARANTINE_DIRNAME,
        )
        claude_after = (
            claude_before
            if claude_update is None
            else _text_snapshot(claude_update.after)
        )
        # Store every postimage before any non-manifest metadata publication.
        journal = _update_transaction_journal(
            journal,
            _journal_with_metadata(
                journal,
                manifest_after=next_manifest,
                claude_after=claude_after,
            ),
        )
        _transaction_fault("quarantine-journal-ready")
        if claude_update is not None:
            _apply_text_update(claude_update)

        _transaction_fault("quarantine-before-final-validation")
        expected_claude = claude_before if claude_update is None else claude_after
        manifest_after_snapshot = journal.data["manifest"]["after"]
        if manifest_after_snapshot is None:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                "ready quarantine journal lacks manifest.after",
            )

        def validate_quarantine_boundary(
            expected_manifest_snapshot: Mapping[str, Any],
        ) -> None:
            _require_quarantine_commit_state(
                analysis,
                moves,
                manifest_before,
                expected_manifest=next_manifest,
                manifest_snapshot=expected_manifest_snapshot,
            )
            _require_snapshot_state(
                journal.path,
                journal.quarantine_root,
                "transaction journal",
                journal.file_snapshot,
                conflict_code="recovery-journal-conflict",
            )
            _require_snapshot_state(
                analysis.root / "CLAUDE.md",
                analysis.root,
                "CLAUDE.md",
                expected_claude,
                conflict_code="claude-doc-index-changed",
            )

        validate_quarantine_boundary(_text_snapshot(manifest_before))
        manifest_disposition = _commit_manifest(
            manifest_path,
            next_manifest,
            journal,
            precommit_validator=lambda: validate_quarantine_boundary(
                _text_snapshot(manifest_before)
            ),
            postcommit_validator=lambda: validate_quarantine_boundary(
                manifest_after_snapshot
            ),
        )
        if manifest_disposition is not ManifestCommitDisposition.DURABLE:
            raise Phase4OperationalError(
                "manifest-durability-unknown",
                f"manifest commit disposition is {manifest_disposition.value}",
            )
        _transaction_fault("quarantine-manifest-committed")
        try:
            _clear_transaction_journal(journal)
        except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
            warnings_out.append(
                f"completed quarantine retained its transaction journal; run --recover before another write: {cleanup_exc.message}"
            )
        return [move.receipt for move in moves], next_manifest, warnings_out
    except Exception as exc:
        if isinstance(exc, Phase4OperationalError) and exc.code in {
            "manifest-durability-unknown",
            "manifest-postcommit-conflict",
        }:
            raise
        rollback_errors: list[str] = []
        if journal is not None:
            try:
                relation = _journal_manifest_relation(journal, analysis.docs_root)
                if relation != "before":
                    raise Phase4Refusal(
                        "recovery-conflict",
                        "manifest commit state forbids an automatic quarantine rollback",
                    )
                _recover_quarantine_before_manifest(
                    journal,
                    analysis.root,
                    analysis.docs_root,
                )
            except (Phase4Refusal, Phase4OperationalError) as rollback_exc:
                rollback_errors.append(rollback_exc.message)
        elif any(
            move.moved or _lexists(move.staging_dir) or _lexists(move.final_dir)
            for move in moves
        ):
            rollback_errors.append(
                "quarantine filesystem changed before a validated journal was retained"
            )
        rollback_errors.extend(_restore_manifest_before(manifest_path, manifest_before))
        if not rollback_errors and journal is not None:
            try:
                _clear_transaction_journal(journal)
            except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
                rollback_errors.append(
                    f"transaction journal retained for recovery: {cleanup_exc.message}"
                )
        if rollback_errors:
            raise Phase4OperationalError(
                "quarantine-rollback-failed",
                f"quarantine failed ({exc}); rollback errors: {'; '.join(rollback_errors)}",
            ) from exc
        if isinstance(exc, Phase4Refusal):
            raise
        if (
            isinstance(exc, Phase4OperationalError)
            and exc.code != "recovery-journal-write-failed"
        ):
            raise
        raise Phase4OperationalError(
            "quarantine-transaction-failed",
            f"quarantine batch failed and was rolled back: {exc}",
        ) from exc


def _append_failure(
    result: dict[str, Any],
    error: Phase4Refusal | Phase4OperationalError,
) -> tuple[dict[str, Any], int]:
    result["ok"] = False
    result["errors"].append({"code": error.code, "message": error.message})
    exit_code = (
        EXIT_REFUSED if isinstance(error, Phase4Refusal) else EXIT_OPERATIONAL_FAILURE
    )
    return result, exit_code


def apply_quarantine(root_value: str | os.PathLike[str]) -> tuple[dict[str, Any], int]:
    """在固定项目锁内全量预校验并提交一个批量 quarantine 事务。"""

    root, _ = _resolve_project(root_value)
    try:
        with contract.project_sync_lock(root, timeout=10.0):
            blocked = _block_if_pending_recovery(root, "apply", "quarantine")
            if blocked is not None:
                return blocked
            fresh = analyze_project(root)
            result = _analysis_result(fresh, "apply", "quarantine")
            result["counts"]["quarantined"] = 0
            if not fresh.safe:
                result["ok"] = False
                return result, EXIT_REFUSED
            if not fresh.candidates:
                result["ok"] = True
                result["counts"]["quarantined"] = 0
                return result, EXIT_OK

            try:
                # 在首次移动前完成所有候选的全量复核。
                expected_active = set(fresh.active_paths)
                validated = [
                    (
                        candidate,
                        _revalidate_candidate(fresh, candidate, expected_active),
                    )
                    for candidate in fresh.candidates
                ]
                receipts, next_manifest, transaction_warnings = _batch_quarantine(
                    fresh,
                    validated,
                )
            except (Phase4Refusal, Phase4OperationalError) as exc:
                return _append_failure(result, exc)

            result["quarantined"] = [
                {
                    "receipt_id": receipt["receipt_id"],
                    "original_path": receipt["original_path"],
                    "sha256": receipt["sha256"],
                    "reason": receipt["reason"],
                    "generation": receipt["generation"],
                    "state": receipt["state"],
                }
                for receipt in receipts
            ]
            result["counts"]["quarantined"] = len(receipts)
            result["manifest_generation"] = next_manifest["generation_id"]
            result["warnings"].extend(transaction_warnings)
            result["ok"] = True
            return result, EXIT_OK
    except contract.LockError as exc:
        raise Phase4OperationalError(
            "project-sync-lock-failed",
            f"cannot acquire or release the project sync lock: {exc}",
        ) from exc


def _validate_receipt_id(receipt_id: str) -> None:
    if not isinstance(receipt_id, str) or _RECEIPT_ID_RE.fullmatch(receipt_id) is None:
        raise Phase4Refusal(
            "invalid-receipt-id",
            f"receipt ID is not a safe identifier: {receipt_id!r}",
        )


def _validate_timestamp(
    value: Any,
    field_name: str,
    *,
    error_code: str = "receipt-invalid",
) -> str:
    if not isinstance(value, str) or not value:
        raise Phase4Refusal(error_code, f"{field_name} must be a timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Phase4Refusal(
            error_code,
            f"{field_name} is not a valid ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise Phase4Refusal(
            error_code,
            f"{field_name} must include a timezone",
        )
    return value


def _validate_serialized_identity(
    value: Any,
    field_name: str,
) -> tuple[int, int, int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(item) is not int or item < 0 for item in value)
    ):
        raise Phase4Refusal(
            "receipt-invalid",
            f"{field_name} must contain four non-negative integers",
        )
    return tuple(value)


def _receipt_payload_identity(
    receipt: Mapping[str, Any],
    *,
    error_code: str,
    context: str,
) -> tuple[int, int, int, int]:
    if receipt.get("schema_version") != 2:
        raise Phase4Refusal(
            error_code,
            f"{context} lacks payload ownership identity",
        )
    try:
        return _validate_serialized_identity(
            receipt.get("payload_identity"),
            f"{context} payload_identity",
        )
    except Phase4Refusal as exc:
        raise Phase4Refusal(error_code, exc.message) from exc


def _validate_receipt_data(
    raw_receipt: Mapping[str, Any], receipt_id: str
) -> dict[str, Any]:
    """Validate the immutable receipt schema without accessing its payload path."""

    base_fields = {
        "created_at",
        "generation",
        "original_path",
        "quarantined_path",
        "reason",
        "receipt_id",
        "restored_at",
        "schema_version",
        "sha256",
        "state",
    }
    schema = raw_receipt.get("schema_version")
    if type(schema) is not int or schema not in (1, 2):
        raise Phase4Refusal("receipt-invalid", "unsupported receipt schema_version")
    expected_fields = base_fields if schema == 1 else base_fields | {"payload_identity"}
    if set(raw_receipt) != expected_fields:
        raise Phase4Refusal(
            "receipt-invalid", "receipt has unsupported or missing fields"
        )
    receipt = dict(raw_receipt)
    if schema == 2:
        _validate_serialized_identity(
            receipt.get("payload_identity"),
            "receipt payload_identity",
        )
    if receipt.get("receipt_id") != receipt_id:
        raise Phase4Refusal(
            "receipt-invalid", "receipt_id does not match its directory"
        )
    original = receipt.get("original_path")
    try:
        pure_original = contract.validate_manifest_relative_path(original)
    except contract.PathValidationError as exc:
        raise Phase4Refusal("receipt-invalid", str(exc)) from exc
    if _is_quarantine_relative(pure_original.as_posix()):
        raise Phase4Refusal(
            "receipt-invalid", "receipt original_path must not point into quarantine"
        )
    expected_quarantined = f"{PAYLOAD_DIRNAME}/{pure_original.as_posix()}"
    if receipt.get("quarantined_path") != expected_quarantined:
        raise Phase4Refusal(
            "receipt-invalid",
            "quarantined_path does not match the canonical receipt payload path",
        )
    try:
        contract.validate_manifest_relative_path(receipt["quarantined_path"])
    except contract.PathValidationError as exc:
        raise Phase4Refusal("receipt-invalid", str(exc)) from exc
    digest = receipt.get("sha256")
    generation = receipt.get("generation")
    reason = receipt.get("reason")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise Phase4Refusal("receipt-invalid", "receipt sha256 is invalid")
    if not isinstance(generation, str) or _SHA256_RE.fullmatch(generation) is None:
        raise Phase4Refusal("receipt-invalid", "receipt generation is invalid")
    if not isinstance(reason, str) or not reason:
        raise Phase4Refusal("receipt-invalid", "receipt reason must be non-empty")
    _validate_timestamp(receipt.get("created_at"), "receipt created_at")
    state = receipt.get("state")
    restored_at = receipt.get("restored_at")
    if state not in ("quarantined", "restored"):
        raise Phase4Refusal(
            "receipt-invalid", "receipt state must be 'quarantined' or 'restored'"
        )
    if state == "quarantined":
        if restored_at is not None:
            raise Phase4Refusal(
                "receipt-invalid",
                "a quarantined receipt must have restored_at=null",
            )
    else:
        _validate_timestamp(restored_at, "receipt restored_at")
    return receipt


def _load_receipt(quarantine_root: Path, receipt_id: str) -> ReceiptRecord:
    _validate_receipt_id(receipt_id)
    receipt_dir = quarantine_root / receipt_id
    if not _lexists(receipt_dir):
        raise Phase4Refusal(
            "receipt-not-found",
            f"receipt does not exist: {receipt_id}",
        )
    _validate_existing_components(quarantine_root, receipt_dir, "receipt directory")
    try:
        resolved_receipt_dir = receipt_dir.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "receipt-invalid",
            f"receipt directory cannot be resolved: {receipt_id}",
        ) from exc
    _require_below(resolved_receipt_dir, quarantine_root, "receipt directory")
    if not resolved_receipt_dir.is_dir():
        raise Phase4Refusal(
            "receipt-invalid",
            f"receipt path is not a directory: {receipt_id}",
        )

    receipt_path = resolved_receipt_dir / RECEIPT_FILENAME
    if not _lexists(receipt_path) or _is_link_or_junction(receipt_path):
        raise Phase4Refusal(
            "receipt-invalid",
            f"receipt JSON is missing or untrusted: {receipt_path}",
        )
    try:
        raw_receipt = contract.read_json_strict(receipt_path)
    except (contract.ProjectDocsContractError, OSError) as exc:
        raise Phase4Refusal("receipt-invalid", str(exc)) from exc
    if not isinstance(raw_receipt, dict):
        raise Phase4Refusal("receipt-invalid", "receipt JSON root must be an object")
    receipt = _validate_receipt_data(dict(raw_receipt), receipt_id)

    if type(receipt.get("schema_version")) is not int or receipt[
        "schema_version"
    ] not in (1, 2):
        raise Phase4Refusal("receipt-invalid", "unsupported receipt schema_version")
    if receipt.get("receipt_id") != receipt_id:
        raise Phase4Refusal(
            "receipt-invalid", "receipt_id does not match its directory"
        )

    original = receipt.get("original_path")
    try:
        pure_original = contract.validate_manifest_relative_path(original)
    except contract.PathValidationError as exc:
        raise Phase4Refusal("receipt-invalid", str(exc)) from exc
    if _is_quarantine_relative(pure_original.as_posix()):
        raise Phase4Refusal(
            "receipt-invalid",
            "receipt original_path must not point into quarantine",
        )

    quarantined = receipt.get("quarantined_path")
    expected_quarantined = f"{PAYLOAD_DIRNAME}/{pure_original.as_posix()}"
    if quarantined != expected_quarantined:
        raise Phase4Refusal(
            "receipt-invalid",
            "quarantined_path does not match the canonical receipt payload path",
        )
    try:
        pure_quarantined = contract.validate_manifest_relative_path(quarantined)
    except contract.PathValidationError as exc:
        raise Phase4Refusal("receipt-invalid", str(exc)) from exc

    digest = receipt.get("sha256")
    generation = receipt.get("generation")
    reason = receipt.get("reason")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise Phase4Refusal("receipt-invalid", "receipt sha256 is invalid")
    if not isinstance(generation, str) or _SHA256_RE.fullmatch(generation) is None:
        raise Phase4Refusal("receipt-invalid", "receipt generation is invalid")
    if not isinstance(reason, str) or not reason:
        raise Phase4Refusal("receipt-invalid", "receipt reason must be non-empty")
    _validate_timestamp(receipt.get("created_at"), "receipt created_at")

    state = receipt.get("state")
    restored_at = receipt.get("restored_at")
    if state not in ("quarantined", "restored"):
        raise Phase4Refusal(
            "receipt-invalid",
            "receipt state must be 'quarantined' or 'restored'",
        )
    if state == "quarantined":
        if restored_at is not None:
            raise Phase4Refusal(
                "receipt-invalid",
                "a quarantined receipt must have restored_at=null",
            )
    else:
        _validate_timestamp(restored_at, "receipt restored_at")

    payload = resolved_receipt_dir.joinpath(*pure_quarantined.parts)
    _validate_existing_components(resolved_receipt_dir, payload, "quarantined payload")
    payload_exists = _lexists(payload)
    if state == "quarantined" and not payload_exists:
        raise Phase4Refusal(
            "receipt-payload-missing",
            f"quarantined payload is missing: {payload}",
        )
    payload_identity: tuple[int, int, int, int] | None = None
    if payload_exists:
        if _is_link_or_junction(payload):
            raise Phase4Refusal(
                "receipt-payload-missing",
                f"quarantined payload is untrusted: {payload}",
            )
        try:
            resolved_payload = payload.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise Phase4Refusal(
                "receipt-payload-missing",
                f"quarantined payload cannot be resolved: {payload}",
            ) from exc
        _require_below(resolved_payload, resolved_receipt_dir, "quarantined payload")
        try:
            payload_snapshot, payload_identity = _capture_snapshot_with_identity(
                resolved_payload,
                resolved_receipt_dir,
                f"receipt payload {receipt_id}",
            )
        except Phase4Refusal as exc:
            raise Phase4Refusal("receipt-payload-invalid", exc.message) from exc
        if payload_snapshot["sha256"] != digest:
            raise Phase4Refusal(
                "receipt-hash-mismatch",
                f"quarantined payload hash differs from the receipt: {receipt_id}",
            )
        payload = resolved_payload
    else:
        payload_snapshot = _absent_snapshot()
    persisted_payload_identity = (
        None
        if receipt["schema_version"] == 1
        else _validate_serialized_identity(
            receipt.get("payload_identity"),
            "receipt payload_identity",
        )
    )
    if (
        payload_identity is not None
        and persisted_payload_identity is not None
        and payload_identity != persisted_payload_identity
    ):
        raise Phase4Refusal(
            "receipt-payload-identity-mismatch",
            "quarantined payload identity differs from the receipt",
        )
    receipt_snapshot, _ = _capture_snapshot_with_identity(
        receipt_path,
        resolved_receipt_dir,
        f"receipt metadata {receipt_id}",
    )
    try:
        receipt_from_snapshot = contract.parse_json_strict(
            receipt_snapshot["text"],
            source=f"receipt metadata {receipt_id}",
        )
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("receipt-invalid", str(exc)) from exc
    if not isinstance(receipt_from_snapshot, Mapping):
        raise Phase4Refusal("receipt-invalid", "receipt JSON root must be an object")
    if _validate_receipt_data(receipt_from_snapshot, receipt_id) != receipt:
        raise Phase4Refusal(
            "receipt-invalid",
            "receipt metadata changed while it was being loaded",
        )
    member_identities: dict[str, tuple[int, int, int, int]] = {}
    directory_identity = _require_closed_transaction_directory(
        resolved_receipt_dir,
        quarantine_root,
        {
            RECEIPT_FILENAME: receipt_snapshot,
            quarantined: payload_snapshot,
        },
        file_identities=member_identities,
    )
    if (
        payload_identity is not None
        and member_identities.get(quarantined) != payload_identity
    ):
        raise Phase4Refusal(
            "receipt-payload-identity-mismatch",
            "quarantined payload identity changed while loading the receipt",
        )
    return ReceiptRecord(
        receipt,
        resolved_receipt_dir,
        payload,
        directory_identity,
        persisted_payload_identity,
    )


def _restore_destination_path(
    docs_root: Path,
    original: str,
    *,
    create_parent: bool = False,
) -> Path:
    """Resolve a restore target without creating it before its journal exists."""

    try:
        destination = contract.resolve_manifest_document(
            docs_root,
            original,
            must_exist=False,
        ).resolved_path
        _validate_existing_components(docs_root, destination, "restore destination")
        if create_parent:
            _ensure_directory_chain(
                docs_root,
                destination.parent,
                "restore destination parent",
            )
            _validate_existing_components(docs_root, destination, "restore destination")
        return destination
    except Phase4Refusal:
        raise
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal("restore-path-invalid", str(exc)) from exc
    except OSError as exc:
        raise Phase4OperationalError(
            "restore-parent-create-failed",
            f"cannot create restore parent for {original}: {exc}",
        ) from exc


def _validate_restored_destination(docs_root: Path, record: ReceiptRecord) -> Path:
    original = record.data["original_path"]
    try:
        destination = contract.resolve_manifest_document(
            docs_root,
            original,
            must_exist=True,
        ).resolved_path
        _validate_existing_components(docs_root, destination, "restored destination")
        contract.read_utf8(destination)
        if contract.sha256_file(destination) != record.data["sha256"]:
            raise OSError("restored destination hash differs from receipt")
        return destination
    except (contract.ProjectDocsContractError, OSError, RuntimeError) as exc:
        raise Phase4Refusal(
            "restore-conflict",
            f"restored receipt destination is missing, changed, or untrusted: {original}: {exc}",
        ) from exc


def _require_exclusive_copy_state(
    created: ExclusiveCopy,
    docs_root: Path,
    expected: Mapping[str, Any],
) -> None:
    """复核 restore 目标仍是本事务独占创建的同一文件。"""

    _validate_existing_components(docs_root, created.path, "restore destination")
    if _is_link_or_junction(created.path) or not created.path.is_file():
        raise Phase4Refusal(
            "restore-conflict",
            f"restore destination became indirect or non-regular: {created.path}",
        )
    try:
        current_identity = _identity_from_stat(created.path.lstat())
    except OSError as exc:
        raise Phase4Refusal(
            "restore-conflict",
            f"restore destination cannot be inspected: {created.path}: {exc}",
        ) from exc
    if current_identity != created.identity:
        raise Phase4Refusal(
            "restore-conflict",
            f"restore destination identity changed: {created.path}",
        )
    _require_snapshot_state(
        created.path,
        docs_root,
        "restore destination",
        expected,
        conflict_code="restore-conflict",
    )


def _require_restore_target_identity(
    target: Path,
    docs_root: Path,
    expected_identity: tuple[int, int, int, int],
    anchor: Path,
    quarantine_root: Path,
    *,
    code: str = "restore-conflict",
) -> tuple[int, int, int, int]:
    """Require the independently published target identity and an absent staging name."""

    target_snapshot, target_identity = _capture_snapshot_with_identity(
        target,
        docs_root,
        "restore target",
    )
    if not target_snapshot["exists"] or target_identity != expected_identity:
        raise Phase4Refusal(
            code,
            f"restore target identity differs from its persisted transaction identity: {target}",
        )
    _require_snapshot_state(
        anchor,
        quarantine_root,
        "restore staged target",
        _absent_snapshot(),
        conflict_code=code,
    )
    return target_identity


def _require_receipt_closed_state(
    record: ReceiptRecord,
    receipt_expected: Mapping[str, Any],
    payload_expected: Mapping[str, Any],
) -> None:
    file_identities: dict[str, tuple[int, int, int, int]] = {}
    identity = _require_closed_transaction_directory(
        record.receipt_dir,
        record.receipt_dir.parent,
        {
            RECEIPT_FILENAME: receipt_expected,
            record.data["quarantined_path"]: payload_expected,
        },
        file_identities=file_identities,
    )
    if identity != record.directory_identity:
        raise Phase4Refusal(
            "restore-conflict",
            f"receipt directory identity changed: {record.receipt_dir}",
        )
    if payload_expected["exists"] and (
        record.payload_identity is None
        or file_identities.get(record.data["quarantined_path"])
        != record.payload_identity
    ):
        raise Phase4Refusal(
            "restore-conflict",
            "receipt payload identity changed",
        )


def _require_restore_commit_state(
    analysis: Analysis,
    record: ReceiptRecord,
    created: ExclusiveCopy,
    target_identity: tuple[int, int, int, int],
    manifest_before: str,
    receipt_expected: Mapping[str, Any],
    payload_expected: Mapping[str, Any],
    anchor_path: Path,
    *,
    expected_manifest: Mapping[str, Any] | None = None,
    manifest_snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """在 restore metadata/manifest 发布相邻位置复核全部权威状态。"""

    manifest_path = analysis.docs_root / contract.MANIFEST_FILENAME
    _require_snapshot_state(
        manifest_path,
        analysis.docs_root,
        "manifest",
        (
            _text_snapshot(manifest_before)
            if manifest_snapshot is None
            else manifest_snapshot
        ),
        conflict_code="manifest-changed",
    )
    expected_target = _text_snapshot(payload_expected["text"])
    if record.payload_identity is None or target_identity == record.payload_identity:
        raise Phase4Refusal(
            "restore-conflict",
            "restore target must not alias the quarantine payload identity",
        )
    _require_exclusive_copy_state(created, analysis.docs_root, expected_target)
    current_identity = _require_restore_target_identity(
        created.path,
        analysis.docs_root,
        target_identity,
        anchor_path,
        record.receipt_dir.parent,
    )
    if current_identity != created.identity:
        raise Phase4Refusal(
            "restore-conflict",
            "restore target identity no longer matches the current transaction",
        )
    expected_active = analysis.active_paths | {record.data["original_path"]}
    current_active = _discover_active_documents(analysis.docs_root)
    if current_active != expected_active:
        raise Phase4Refusal(
            "active-inventory-changed",
            "active document inventory changed before restore commit "
            f"(added={sorted(current_active - expected_active)}, "
            f"removed={sorted(expected_active - current_active)})",
        )
    receipt_path = record.receipt_dir / RECEIPT_FILENAME
    _require_snapshot_state(
        receipt_path,
        record.receipt_dir,
        "restore receipt",
        receipt_expected,
        conflict_code="restore-conflict",
    )
    _require_snapshot_state(
        record.payload_path,
        record.receipt_dir,
        "restore payload",
        payload_expected,
        conflict_code="restore-conflict",
    )
    _require_snapshot_state(
        anchor_path,
        record.receipt_dir.parent,
        "restore staged target",
        _absent_snapshot(),
        conflict_code="restore-conflict",
    )
    _require_receipt_closed_state(record, receipt_expected, payload_expected)
    if expected_manifest is None:
        return _build_current_manifest(analysis)
    try:
        return contract.validate_manifest(
            dict(expected_manifest),
            analysis.docs_root,
            verify_files=True,
            verify_complete_set=True,
        )
    except contract.ProjectDocsContractError as exc:
        raise Phase4Refusal(
            "manifest-postimage-stale",
            f"restore manifest no longer matches the active document tree: {exc}",
        ) from exc


def _restore_transaction(
    analysis: Analysis,
    record: ReceiptRecord,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Restore one receipt through a durable write-ahead journal."""

    original = record.data["original_path"]
    if record.payload_identity is None:
        raise Phase4Refusal(
            "receipt-identity-unavailable",
            "legacy receipt payload identity is unavailable; automatic restore is unsafe",
        )
    manifest_path = analysis.docs_root / contract.MANIFEST_FILENAME
    manifest_before = _manifest_snapshot_text(analysis)
    receipt_path = record.receipt_dir / RECEIPT_FILENAME
    receipt_before = _capture_snapshot(
        receipt_path, record.receipt_dir, "restore receipt"
    )
    payload_before = _capture_snapshot(
        record.payload_path, record.receipt_dir, "restore payload"
    )
    destination = _restore_destination_path(analysis.docs_root, original)
    claude_before = _capture_snapshot(
        analysis.root / "CLAUDE.md", analysis.root, "CLAUDE.md"
    )
    updated_receipt = dict(record.data)
    updated_receipt["state"] = "restored"
    updated_receipt["restored_at"] = _utc_now()
    anchor_path = _restore_anchor_path(
        record.receipt_dir.parent,
        record.data["receipt_id"],
    )
    anchor_copy: ExclusiveCopy | None = None
    created: ExclusiveCopy | None = None
    target_identity: tuple[int, int, int, int] | None = None
    claude_update: TextUpdate | None = None
    receipt_update: TextUpdate | None = None
    journal: TransactionJournal | None = None
    warnings_out: list[str] = []

    try:
        # Parent creation and the exclusive copy are deliberately after this write.
        journal = _write_transaction_journal(
            record.receipt_dir.parent,
            _restore_journal_prepared(
                manifest_before=manifest_before,
                claude_before=claude_before,
                record=record,
                receipt_before=receipt_before,
                payload_before=payload_before,
                updated_receipt=updated_receipt,
            ),
        )
        _transaction_fault("restore-journal-created")
        destination = _restore_destination_path(
            analysis.docs_root,
            original,
            create_parent=True,
        )
        anchor_copy = _write_exclusive_snapshot(
            anchor_path,
            record.receipt_dir.parent,
            "restore staged target",
            payload_before,
        )
        _transaction_fault("restore-staged-target-created")
        if anchor_copy.identity == record.payload_identity:
            raise Phase4Refusal(
                "restore-conflict",
                "restore staged target aliases the quarantine payload identity",
            )
        target_identity = anchor_copy.identity
        journal = _update_restore_target_journal(journal, target_identity)
        _transaction_fault("restore-target-journal-created")
        try:
            created = _exclusive_copy_file(
                anchor_path,
                destination,
                record.data["sha256"],
                target_identity,
                archive_root=record.receipt_dir.parent,
                archive_trusted_root=analysis.docs_root,
            )
        except FileExistsError as exc:
            raise Phase4Refusal(
                "restore-conflict",
                f"restore destination already exists: {original}",
            ) from exc
        _transaction_fault("restore-copy-complete")

        next_manifest = _require_restore_commit_state(
            analysis,
            record,
            created,
            target_identity,
            manifest_before,
            receipt_before,
            payload_before,
            anchor_path,
        )
        claude_update = _prepare_claude_update(
            analysis.root,
            next_manifest,
            archive_root=analysis.docs_root / QUARANTINE_DIRNAME,
        )
        claude_after = (
            claude_before
            if claude_update is None
            else _text_snapshot(claude_update.after)
        )
        journal = _update_transaction_journal(
            journal,
            _journal_with_metadata(
                journal,
                manifest_after=next_manifest,
                claude_after=claude_after,
            ),
        )
        _transaction_fault("restore-journal-ready")
        if claude_update is not None:
            _apply_text_update(claude_update)

        receipt_update = TextUpdate(
            path=receipt_path,
            before=receipt_before["text"],
            after=contract.stable_json_dumps(updated_receipt),
            archive_root=record.receipt_dir.parent,
            archive_trusted_root=analysis.docs_root,
        )
        _apply_json_update(receipt_update, updated_receipt)

        _transaction_fault("restore-before-final-validation")
        receipt_after = _text_snapshot(receipt_update.after)
        expected_claude = claude_before if claude_update is None else claude_after
        manifest_after_snapshot = journal.data["manifest"]["after"]
        if manifest_after_snapshot is None:
            raise Phase4Refusal(
                "recovery-journal-invalid",
                "ready restore journal lacks manifest.after",
            )

        def validate_restore_boundary(
            expected_manifest_snapshot: Mapping[str, Any],
        ) -> None:
            _require_restore_commit_state(
                analysis,
                record,
                created,
                target_identity,
                manifest_before,
                receipt_after,
                payload_before,
                anchor_path,
                expected_manifest=next_manifest,
                manifest_snapshot=expected_manifest_snapshot,
            )
            _require_snapshot_state(
                journal.path,
                journal.quarantine_root,
                "transaction journal",
                journal.file_snapshot,
                conflict_code="recovery-journal-conflict",
            )
            _require_snapshot_state(
                analysis.root / "CLAUDE.md",
                analysis.root,
                "CLAUDE.md",
                expected_claude,
                conflict_code="claude-doc-index-changed",
            )

        validate_restore_boundary(_text_snapshot(manifest_before))
        manifest_disposition = _commit_manifest(
            manifest_path,
            next_manifest,
            journal,
            precommit_validator=lambda: validate_restore_boundary(
                _text_snapshot(manifest_before)
            ),
            postcommit_validator=lambda: validate_restore_boundary(
                manifest_after_snapshot
            ),
        )
        if manifest_disposition is not ManifestCommitDisposition.DURABLE:
            raise Phase4OperationalError(
                "manifest-durability-unknown",
                f"manifest commit disposition is {manifest_disposition.value}",
            )
        _transaction_fault("restore-manifest-committed")
    except Exception as exc:
        if isinstance(exc, Phase4OperationalError) and exc.code in {
            "manifest-durability-unknown",
            "manifest-postcommit-conflict",
        }:
            raise
        rollback_errors = _rollback_text_update(receipt_update)
        rollback_errors.extend(_rollback_text_update(claude_update))
        rollback_errors.extend(_restore_manifest_before(manifest_path, manifest_before))
        if created is not None and not rollback_errors:
            try:
                _unlink_exclusive_copy(created)
            except BaseException as remove_exc:  # pragma: no cover - catastrophic race
                rollback_errors.append(f"remove restore destination: {remove_exc}")
        if anchor_copy is not None and not rollback_errors:
            if anchor_copy.identity == record.payload_identity and _lexists(
                anchor_copy.path
            ):
                rollback_errors.append(
                    "restore staged target aliases the receipt payload; retained for recovery"
                )
            else:
                try:
                    _unlink_exclusive_copy(anchor_copy)
                except (
                    BaseException
                ) as remove_exc:  # pragma: no cover - catastrophic race
                    rollback_errors.append(
                        f"remove restore staged target: {remove_exc}"
                    )
        if not rollback_errors and journal is not None:
            target_now, target_now_identity = _capture_snapshot_with_identity(
                destination,
                analysis.docs_root,
                "restore destination",
            )
            anchor_now = _capture_snapshot(
                anchor_path,
                record.receipt_dir.parent,
                "restore staged target",
            )
            owned_target_remains = (
                target_now["exists"]
                and target_identity is not None
                and target_now_identity == target_identity
            )
            if not owned_target_remains and not anchor_now["exists"]:
                try:
                    _clear_transaction_journal(journal)
                except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
                    rollback_errors.append(
                        f"transaction journal retained for recovery: {cleanup_exc.message}"
                    )
            else:
                rollback_errors.append(
                    "transaction-owned restore target or staging path remains; "
                    "journal retained for recovery"
                )
        if rollback_errors:
            raise Phase4OperationalError(
                "restore-rollback-failed",
                f"restore failed ({exc}); rollback errors: {'; '.join(rollback_errors)}",
            ) from exc
        if isinstance(exc, Phase4Refusal):
            raise
        if isinstance(exc, Phase4OperationalError):
            raise
        raise Phase4OperationalError(
            "restore-transaction-failed",
            f"restore transaction failed for {original} and was rolled back: {exc}",
        ) from exc

    # Manifest 已提交；仅当 target/receipt/payload 仍是精确事务状态时清理冗余载荷。
    cleanup_ready = True
    try:
        _transaction_fault("restore-before-payload-cleanup")
        _require_exclusive_copy_state(
            created,
            analysis.docs_root,
            _text_snapshot(payload_before["text"]),
        )
        _require_snapshot_state(
            receipt_path,
            record.receipt_dir,
            "restore receipt",
            receipt_after,
            conflict_code="restore-conflict",
        )
        _require_snapshot_state(
            record.payload_path,
            record.receipt_dir,
            "restore payload",
            payload_before,
            conflict_code="restore-conflict",
        )
        _require_restore_target_identity(
            created.path,
            analysis.docs_root,
            target_identity,
            anchor_path,
            record.receipt_dir.parent,
        )
        if target_identity == record.payload_identity:
            raise Phase4Refusal(
                "restore-conflict",
                "restore target aliases the duplicate quarantine payload",
            )
        _require_receipt_closed_state(record, receipt_after, payload_before)
        _validate_manifest_snapshot(
            analysis.docs_root,
            _text_snapshot(contract.stable_json_dumps(next_manifest)),
        )
    except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
        cleanup_ready = False
        warnings_out.append(
            "restored transaction state changed after manifest commit; retained its "
            f"payload and journal for explicit recovery: {cleanup_exc.message}"
        )
    if cleanup_ready:
        try:
            _clear_transaction_journal(journal)
        except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
            warnings_out.append(
                "completed restore retained its duplicate payload and transaction journal; "
                f"run --recover before another write: {cleanup_exc.message}"
            )
        else:
            try:
                _remove_snapshot_file(
                    record.payload_path,
                    record.receipt_dir,
                    "restored duplicate quarantine payload",
                    payload_before,
                    expected_identity=record.payload_identity,
                    archive_root=record.receipt_dir.parent,
                    archive_trusted_root=analysis.docs_root,
                )
            except (Phase4Refusal, Phase4OperationalError) as cleanup_exc:
                warnings_out.append(
                    "restored receipt retained a duplicate quarantine payload; "
                    f"a later no-op restore can retry cleanup: {cleanup_exc.message}"
                )

    restored = {
        "receipt_id": record.data["receipt_id"],
        "original_path": original,
        "sha256": record.data["sha256"],
        "generation": record.data["generation"],
        "state": "restored",
        "restored_at": updated_receipt["restored_at"],
        "no_op": False,
    }
    return restored, next_manifest, warnings_out


def restore_receipt(
    root_value: str | os.PathLike[str],
    receipt_id: str,
) -> tuple[dict[str, Any], int]:
    """在固定项目锁内无覆盖恢复，并原子更新 receipt/manifest。"""

    root, _ = _resolve_project(root_value)
    _validate_receipt_id(receipt_id)
    try:
        with contract.project_sync_lock(root, timeout=10.0):
            root, docs_root = _resolve_project(root)
            blocked = _block_if_pending_recovery(root, "restore", "restore")
            if blocked is not None:
                return blocked
            quarantine_root = _validate_existing_quarantine(docs_root)
            if quarantine_root is None:
                raise Phase4Refusal(
                    "quarantine-missing",
                    "quarantine root does not exist",
                )
            record = _load_receipt(quarantine_root, receipt_id)

            if record.data["state"] == "restored":
                _validate_restored_destination(docs_root, record)
                analysis = analyze_project(root)
                if not analysis.safe:
                    result = _analysis_result(analysis, "restore", "restore")
                    result["ok"] = False
                    return result, EXIT_REFUSED
                entry = analysis.manifest_entries.get(record.data["original_path"])
                if entry is None or entry.get("sha256") != record.data["sha256"]:
                    raise Phase4Refusal(
                        "restore-conflict",
                        "restored destination is not registered with the receipt hash",
                    )
                cleanup_warnings: list[str] = []
                orphan_anchor = _restore_anchor_path(quarantine_root, receipt_id)
                _require_snapshot_state(
                    orphan_anchor,
                    quarantine_root,
                    "completed restore staged target",
                    _absent_snapshot(),
                    conflict_code="restore-conflict",
                )
                if _lexists(record.payload_path):
                    if record.payload_identity is None:
                        cleanup_warnings.append(
                            "legacy restored receipt retained its duplicate quarantine "
                            "payload because ownership identity is unavailable"
                        )
                    else:
                        try:
                            duplicate = _capture_snapshot(
                                record.payload_path,
                                record.receipt_dir,
                                "restored duplicate quarantine payload",
                            )
                            if duplicate["sha256"] != record.data["sha256"]:
                                raise Phase4Refusal(
                                    "restore-conflict",
                                    "duplicate quarantine payload changed before cleanup",
                                )
                            _remove_snapshot_file(
                                record.payload_path,
                                record.receipt_dir,
                                "restored duplicate quarantine payload",
                                duplicate,
                                expected_identity=record.payload_identity,
                                archive_root=quarantine_root,
                                archive_trusted_root=docs_root,
                            )
                        except (Phase4Refusal, Phase4OperationalError) as exc:
                            cleanup_warnings.append(
                                "restored receipt retained a duplicate quarantine payload: "
                                f"{exc}"
                            )
                result = _empty_result("restore", "restore")
                result["ok"] = True
                result["restored"] = {
                    "receipt_id": record.data["receipt_id"],
                    "original_path": record.data["original_path"],
                    "sha256": record.data["sha256"],
                    "generation": record.data["generation"],
                    "state": "restored",
                    "restored_at": record.data["restored_at"],
                    "no_op": True,
                }
                result["warnings"].extend(cleanup_warnings)
                result["counts"] = {"restored": 0, "no_op": 1}
                return result, EXIT_OK

            analysis = analyze_project(root)
            result = _analysis_result(analysis, "restore", "restore")
            result["counts"]["restored"] = 0
            result["counts"]["no_op"] = 0
            if not analysis.safe:
                if record.data["original_path"] in analysis.active_paths:
                    raise Phase4Refusal(
                        "restore-conflict",
                        f"restore destination already exists: {record.data['original_path']}",
                    )
                result["ok"] = False
                return result, EXIT_REFUSED
            try:
                restored, next_manifest, transaction_warnings = _restore_transaction(
                    analysis,
                    record,
                )
            except (Phase4Refusal, Phase4OperationalError) as exc:
                return _append_failure(result, exc)

            result["ok"] = True
            result["restored"] = restored
            result["counts"]["restored"] = 1
            result["counts"]["no_op"] = 0
            result["manifest_generation"] = next_manifest["generation_id"]
            result["warnings"].extend(transaction_warnings)
            return result, EXIT_OK
    except contract.LockError as exc:
        raise Phase4OperationalError(
            "project-sync-lock-failed",
            f"cannot acquire or release the project sync lock: {exc}",
        ) from exc


def report_project(root_value: str | os.PathLike[str]) -> tuple[dict[str, Any], int]:
    root, _ = _resolve_project(root_value)
    try:
        with contract.project_sync_lock(root, timeout=10.0):
            blocked = _block_if_pending_recovery(root, "report", "report-only")
            if blocked is not None:
                return blocked
            analysis = analyze_project(root)
            result = _analysis_result(analysis, "report", "report-only")
            return result, EXIT_OK if analysis.safe else EXIT_REFUSED
    except contract.LockError as exc:
        raise Phase4OperationalError(
            "project-sync-lock-failed",
            f"cannot acquire or release the project sync lock: {exc}",
        ) from exc


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, ValueError, OSError):
                pass


def _emit(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(contract.stable_json_dumps(result))
        return

    status = "OK" if result["ok"] else "REFUSED/FAILED"
    print(f"Phase 4 action: {result['action']} ({result['mode']})")
    print(f"Status: {status}")
    counts = result.get("counts", {})
    if counts:
        print("Counts: " + ", ".join(f"{key}={counts[key]}" for key in sorted(counts)))
    for warning in result.get("warnings", []):
        print(f"Warning: {warning}")
    for error in result.get("errors", []):
        print(f"Error [{error['code']}]: {error['message']}")
    for candidate in result.get("candidates", []):
        print(f"Candidate: {candidate['path']}")
    for blocked in result.get("blocked", []):
        print(f"Blocked [{blocked['reason']}]: {blocked['path']}")
    for receipt in result.get("quarantined", []):
        print(f"Quarantined: {receipt['original_path']} -> {receipt['receipt_id']}")
    if result.get("restored"):
        print(f"Restored: {result['restored']['original_path']}")


def _argument_refusal(
    action: str, mode: str, code: str, message: str
) -> tuple[dict[str, Any], int]:
    result = _empty_result(action, mode)
    result["errors"].append({"code": code, "message": message})
    return result, EXIT_REFUSED


def _execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    action = (
        "recover"
        if args.recover
        else (
            "restore"
            if args.restore is not None
            else "apply" if args.apply else "report"
        )
    )
    mode = (
        "recovery"
        if action == "recover"
        else (
            "restore"
            if action == "restore"
            else "quarantine" if action == "apply" else "report-only"
        )
    )

    if args.dry_run and action != "report":
        return _argument_refusal(
            action,
            mode,
            "incompatible-options",
            "--dry-run cannot be combined with --apply, --restore, or --recover",
        )
    if args.auto_approve is not None and action != "report":
        return _argument_refusal(
            action,
            mode,
            "legacy-option-refused",
            "legacy --auto-approve is report-only and cannot authorize an action",
        )

    if action == "recover":
        return _recover_pending_transaction(args.root)
    if action == "restore":
        return restore_receipt(args.root, args.restore)
    if action == "apply":
        return apply_quarantine(args.root)

    result, exit_code = report_project(args.root)
    if args.auto_approve is not None:
        result["warnings"].append(
            "--auto-approve is deprecated and ignored; this invocation was report-only"
        )
    if args.dry_run:
        result["warnings"].append(
            "--dry-run is a compatibility alias for report-only mode"
        )
    return result, exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 4 fail-closed report, quarantine, and restore tool",
    )
    parser.add_argument("root", help="project root containing project_docs")
    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--apply",
        action="store_true",
        help="quarantine only candidates that pass every safety gate",
    )
    action_group.add_argument(
        "--restore",
        metavar="RECEIPT_ID",
        help="restore one intact quarantine receipt",
    )
    action_group.add_argument(
        "--recover",
        action="store_true",
        help="recover one pending Phase 4 transaction journal",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="legacy compatibility alias for report-only mode",
    )
    parser.add_argument(
        "--auto-approve",
        choices=("A", "A+B"),
        default=None,
        help="deprecated compatibility option; always report-only",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit exactly one JSON document on stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(argv)
    action = (
        "recover"
        if args.recover
        else (
            "restore"
            if args.restore is not None
            else "apply" if args.apply else "report"
        )
    )
    mode = (
        "recovery"
        if action == "recover"
        else (
            "restore"
            if action == "restore"
            else "quarantine" if action == "apply" else "report-only"
        )
    )
    try:
        result, exit_code = _execute(args)
    except Phase4Refusal as exc:
        result = _empty_result(action, mode)
        result["errors"].append({"code": exc.code, "message": exc.message})
        exit_code = EXIT_REFUSED
    except Phase4OperationalError as exc:
        result = _empty_result(action, mode)
        result["errors"].append({"code": exc.code, "message": exc.message})
        exit_code = EXIT_OPERATIONAL_FAILURE
    except (contract.ProjectDocsContractError, OSError, RuntimeError) as exc:
        result = _empty_result(action, mode)
        result["errors"].append(
            {
                "code": "unexpected-safety-failure",
                "message": str(exc),
            }
        )
        exit_code = EXIT_REFUSED
    except Exception as exc:  # pragma: no cover - last-resort stable CLI contract
        result = _empty_result(action, mode)
        result["errors"].append(
            {
                "code": "unexpected-operational-failure",
                "message": str(exc),
            }
        )
        exit_code = EXIT_OPERATIONAL_FAILURE

    result["exit_code"] = exit_code
    result["ok"] = bool(result.get("ok")) and exit_code == EXIT_OK
    _emit(result, as_json=args.json)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
