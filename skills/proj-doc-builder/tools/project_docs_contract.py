# -*- coding: utf-8 -*-
"""Shared, security-sensitive contracts for ``project_docs`` tooling.

The module intentionally has no dependency on the other proj-doc-builder tools so
that writers (the coordinator) and any reader can import the same path, link, JSON,
locking, and manifest rules.
"""

from __future__ import annotations

import concurrent.futures
import copy
import errno
import hashlib
import json
import math
import os
import posixpath
import re
import stat
import sys
import tempfile
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import unquote

MANIFEST_SCHEMA_VERSION = 2
DEFAULT_PRODUCER_NAME = "proj-doc-builder"
DEFAULT_PRODUCER_VERSION = "3.2"
MANIFEST_FILENAME = "_manifest.json"
QUARANTINE_DIRNAME = ".quarantine"
COORDINATOR_JOURNAL_SUFFIX = ".publish-transaction.json"
COORDINATOR_COMMITTED_SUFFIX = ".publish-transaction.committed.json"
COORDINATOR_ROLLED_BACK_SUFFIX = ".publish-transaction.rolled-back.json"
COORDINATOR_TEMP_MARKER = ".publish-"
COORDINATOR_ARTIFACT_ROOT_MARKER = ".publish-artifacts-"
COORDINATOR_RETAINED_ARTIFACT_MARKER = ".publish-retained-"
COORDINATOR_HISTORY_MARKER = ".publish-history-"

SOURCE_FILE_EXTENSIONS = frozenset(
    {
        ".asm",
        ".bash",
        ".bat",
        ".c",
        ".cc",
        ".cfg",
        ".cmake",
        ".cmd",
        ".conf",
        ".cpp",
        ".css",
        ".csv",
        ".cxx",
        ".go",
        ".h",
        ".hh",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".mk",
        ".ps1",
        ".py",
        ".pyi",
        ".pyx",
        ".rs",
        ".scss",
        ".sh",
        ".s",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

_STATUS_MARKERS = ("✅", "🔶", "⬜", "📦", "❓")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_URI_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_WINDOWS_FORBIDDEN_CHARS = frozenset('<>:"|?*')
_WINDOWS_RESERVED_NAME_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"^#(?!#)[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_STATUS_SECTION_RE = re.compile(
    r"^##[ \t]+状态[ \t]*\r?\n(.*?)(?=^#{1,6}[ \t]+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_LAST_SYNCED_RE = re.compile(
    r"<!--\s*Last\s+synced\s*:\s*(.*?)\s*-->",
    re.IGNORECASE | re.DOTALL,
)
_MARKDOWN_LINK_RE = re.compile(
    r"(?<!!)\[([^\]\r\n]*)\]\(\s*"
    r"(<[^>\r\n]*>|[^\s)]+)"
    r"(?:\s+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^()\r\n]*\)))?\s*\)"
)
_MARKDOWN_REFERENCE_DEFINITION_RE = re.compile(
    r"^[ \t]{0,3}\[([^\]\r\n]+)\]:[ \t]*"
    r"(<[^>\r\n]*>|[^\s<>]+)"
    r"(?:[ \t]+(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\([^()\r\n]*\)))?[ \t]*(?:\r?$)",
    re.MULTILINE,
)
_MARKDOWN_REFERENCE_LINK_RE = re.compile(r"(?<!!)\[([^\]\r\n]+)\]\[([^\]\r\n]*)\]")
_MARKDOWN_SHORTCUT_REFERENCE_RE = re.compile(r"(?<!!)\[([^\]\r\n]+)\](?![ \t]*[\[(])")
_HTML_HREF_RE = re.compile(
    r"<a\b[^>]*?\bhref[ \t]*=[ \t]*(?:\"([^\"\r\n]*)\"|'([^'\r\n]*)')",
    re.IGNORECASE,
)

PathLike = str | os.PathLike[str]


class ProjectDocsContractError(Exception):
    """Base class for all explicit contract failures."""


class StrictUtf8Error(ProjectDocsContractError, UnicodeError):
    """A file or value is not valid strict UTF-8."""


class JsonContractError(ProjectDocsContractError, ValueError):
    """JSON is malformed, non-standard, or contains duplicate object keys."""


class PathValidationError(ProjectDocsContractError, ValueError):
    """A document path violates the portable project_docs path contract."""


class PathConflictError(PathValidationError):
    """Two manifest paths are equal after Unicode case folding."""


class LinkResolutionError(ProjectDocsContractError, ValueError):
    """A source document cannot be safely placed below project_docs."""


class AtomicWriteError(ProjectDocsContractError, OSError):
    """An atomic UTF-8 or JSON write failed."""


class LockError(ProjectDocsContractError, OSError):
    """A cross-process lock could not be acquired or released."""


class LockTimeoutError(LockError, TimeoutError):
    """An exclusive lock was not acquired before its deadline."""


class ManifestError(ProjectDocsContractError, ValueError):
    """Base class for manifest failures."""


class ManifestSchemaError(ManifestError):
    """A manifest uses an unsupported schema or malformed schema fields."""


class ManifestValidationError(ManifestError):
    """A supported manifest is internally inconsistent."""


class ManifestFileMismatchError(ManifestValidationError):
    """Manifest bytes or digest metadata does not match a document file."""


class LegacyManifestWarning(UserWarning):
    """A v1 manifest was accepted with untrusted fields recomputed in memory."""


@dataclass(frozen=True)
class SafeManifestPath:
    """A validated manifest path and its resolved location."""

    relative_path: str
    pure_path: PurePosixPath
    resolved_path: Path
    depth: int


@dataclass(frozen=True)
class DocumentMetadata:
    """Metadata parsed from one Markdown document."""

    title: str
    status: str
    synced_at: str | None


class LinkDisposition(str, Enum):
    """Classification returned for every parsed Markdown link."""

    LOCAL_DOCUMENT = "local_document"
    SKIPPED = "skipped"
    INVALID = "invalid"


@dataclass(frozen=True)
class MarkdownLinkResult:
    """Structured Markdown link parse and resolution result."""

    text: str
    raw_target: str
    disposition: LinkDisposition
    source_path: Path
    normalized_target: str | None = None
    project_relative_path: str | None = None
    resolved_path: Path | None = None
    exists: bool | None = None
    reason: str | None = None
    query: str | None = None
    fragment: str | None = None
    span: tuple[int, int] | None = None

    @property
    def is_local_document(self) -> bool:
        """Whether this result names a safe local Markdown document."""

        return self.disposition is LinkDisposition.LOCAL_DOCUMENT


def _is_link_or_reparse_path(path: Path) -> bool:
    """Return whether an existing path is a link, junction, or reparse point."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    is_junction = getattr(path, "is_junction", None)
    try:
        junction = bool(callable(is_junction) and is_junction())
    except OSError as exc:
        raise LockError(f"cannot inspect lock path {path}: {exc}") from exc
    return stat.S_ISLNK(info.st_mode) or junction or bool(attributes & reparse_flag)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Compare stable filesystem identity fields after a no-follow open."""

    first_identity = (getattr(first, "st_dev", 0), getattr(first, "st_ino", 0))
    second_identity = (getattr(second, "st_dev", 0), getattr(second, "st_ino", 0))
    if first_identity != (0, 0) and second_identity != (0, 0):
        return first_identity == second_identity
    return (
        stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and first.st_size == second.st_size
        and getattr(first, "st_ctime_ns", None) == getattr(second, "st_ctime_ns", None)
    )


class ExclusiveFileLock:
    """Small cross-process exclusive lock using verified regular files."""

    def __init__(
        self,
        lock_path: PathLike,
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.path = Path(lock_path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: int | None = None

    def _try_lock(self, fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        elif os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:  # pragma: no cover - supported deployment platforms are nt/posix
            raise LockError(f"unsupported lock platform: {os.name}")

    def _unlock(self, fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

    def acquire(self) -> "ExclusiveFileLock":
        """Acquire the lock, waiting until timeout when another process owns it."""

        if self._fd is not None:
            raise LockError(f"lock is already acquired: {self.path}")
        if not self.path.parent.is_dir():
            raise LockError(f"lock parent directory does not exist: {self.path.parent}")
        if _is_link_or_reparse_path(self.path.parent):
            raise LockError(
                f"lock parent must not be a link or reparse point: {self.path.parent}"
            )

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        expected_identity: os.stat_result | None = None
        try:
            try:
                fd = os.open(
                    self.path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                )
            except FileExistsError:
                if _is_link_or_reparse_path(self.path):
                    raise LockError(
                        f"lock file must not be a link or reparse point: {self.path}"
                    )
                expected_identity = self.path.lstat()
                if not stat.S_ISREG(expected_identity.st_mode):
                    raise LockError(f"lock path is not a regular file: {self.path}")
                fd = os.open(self.path, os.O_RDWR | nofollow)

            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise LockError(f"opened lock is not a regular file: {self.path}")
            opened_attributes = getattr(opened, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if opened_attributes & reparse_flag:
                raise LockError(f"opened lock is a reparse point: {self.path}")
            if expected_identity is not None and not _same_file_identity(
                expected_identity,
                opened,
            ):
                raise LockError(f"lock file changed while being opened: {self.path}")
            if opened.st_size == 0:
                # ``O_EXCL`` 与首次写入之间无法做到单个系统调用原子化。
                # 若创建者在该窗口崩溃，后继进程必须能够安全补齐同一个
                # 固定锁字节，否则零字节文件会永久阻断项目。多个修复者写入
                # 相同字节不会放宽后续 OS 级互斥。
                os.lseek(fd, 0, os.SEEK_SET)
                written = os.write(fd, b"\0")
                if written != 1:
                    raise OSError(f"short lock initialization write: {written}/1")
                os.fsync(fd)
                opened = os.fstat(fd)

            current_identity = self.path.lstat()
            if not _same_file_identity(opened, current_identity):
                raise LockError(f"lock file changed during initialization: {self.path}")
        except LockError:
            if fd is not None:
                os.close(fd)
            raise
        except OSError as exc:
            if fd is not None:
                os.close(fd)
            raise LockError(f"cannot safely open lock file {self.path}: {exc}") from exc

        if fd is None:  # pragma: no cover - all successful branches assign fd
            raise LockError(f"lock file descriptor was not created: {self.path}")
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        try:
            while True:
                try:
                    self._try_lock(fd)
                    self._fd = fd
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise LockError(f"cannot lock {self.path}: {exc}") from exc
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockTimeoutError(
                            f"timed out acquiring exclusive lock: {self.path}"
                        ) from exc
                    time.sleep(self.poll_interval)
        except BaseException:
            os.close(fd)
            raise

    def release(self) -> None:
        """Release this instance's lock and close its file descriptor."""

        fd = self._fd
        if fd is None:
            return
        self._fd = None
        unlock_error: OSError | None = None
        try:
            self._unlock(fd)
        except OSError as exc:
            unlock_error = exc
        finally:
            os.close(fd)
        if unlock_error is not None:
            raise LockError(
                f"cannot unlock {self.path}: {unlock_error}"
            ) from unlock_error

    def __enter__(self) -> "ExclusiveFileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@contextmanager
def exclusive_lock(
    lock_path: PathLike,
    *,
    timeout: float | None = 10.0,
    poll_interval: float = 0.05,
) -> Iterator[ExclusiveFileLock]:
    """Context manager for a Windows ``msvcrt`` / POSIX ``fcntl`` lock."""

    lock = ExclusiveFileLock(
        lock_path,
        timeout=timeout,
        poll_interval=poll_interval,
    )
    with lock:
        yield lock


def exclusive_file_lock(
    lock_path: PathLike,
    *,
    timeout: float | None = 10.0,
    poll_interval: float = 0.05,
) -> Iterator[ExclusiveFileLock]:
    """Alias with an explicit file-lock name for importing callers."""

    return exclusive_lock(
        lock_path,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def project_sync_lock_path(project_root: PathLike) -> Path:
    """Return one process-shared lock path without polluting the project tree."""

    supplied = Path(project_root)
    try:
        root = supplied.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LockError(
            f"project root cannot be resolved for locking: {supplied}"
        ) from exc
    if not root.is_dir():
        raise LockError(f"project root is not a directory: {root}")
    lock_parent = Path(tempfile.gettempdir()) / "proj-doc-builder-locks"
    try:
        lock_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise LockError(
            f"cannot create shared lock directory {lock_parent}: {exc}"
        ) from exc
    if _is_link_or_reparse_path(lock_parent):
        raise LockError(f"shared lock directory is untrusted: {lock_parent}")
    digest = sha256_bytes(os.path.normcase(str(root)).encode("utf-8"))
    return lock_parent / f"{digest}.lock"


def coordinator_publish_journal_paths(project_root: PathLike) -> dict[str, Path]:
    """Return every reserved coordinator journal slot in the shared lock domain."""

    lock_path = project_sync_lock_path(project_root)
    return {
        "prepared": lock_path.with_name(
            f"{lock_path.stem}{COORDINATOR_JOURNAL_SUFFIX}"
        ),
        "committed": lock_path.with_name(
            f"{lock_path.stem}{COORDINATOR_COMMITTED_SUFFIX}"
        ),
        "rolled_back": lock_path.with_name(
            f"{lock_path.stem}{COORDINATOR_ROLLED_BACK_SUFFIX}"
        ),
    }


def coordinator_pending_publish_status(
    project_root: PathLike,
) -> dict[str, Any] | None:
    """Detect journals and crash residues without parsing coordinator internals.

    Every caller holds the same project lock while acting on this result.  A temp
    residue is treated as pending rather than deleted because its ownership cannot
    be inferred from its pathname after a process crash.
    """

    slots = coordinator_publish_journal_paths(project_root)
    parent = next(iter(slots.values())).parent
    if _is_link_or_reparse_path(parent):
        raise LockError(f"coordinator journal directory is untrusted: {parent}")
    try:
        entries = list(parent.iterdir())
    except OSError as exc:
        raise LockError(
            f"cannot inspect coordinator journal directory {parent}: {exc}"
        ) from exc

    slot_by_name = {path.name: phase for phase, path in slots.items()}
    prepared_name = slots["prepared"].name
    temp_prefix = f"{prepared_name}{COORDINATOR_TEMP_MARKER}"
    artifact_prefix = (
        f"{slots['prepared'].stem}{COORDINATOR_ARTIFACT_ROOT_MARKER}"
    )
    history_prefix = (
        f"{slots['prepared'].stem}{COORDINATOR_HISTORY_MARKER}"
    )
    found: list[dict[str, str]] = []
    for entry in entries:
        phase = slot_by_name.get(entry.name)
        is_temporary = entry.name.startswith(temp_prefix) and entry.name.endswith(
            ".tmp"
        )
        is_artifact_root = entry.name.startswith(artifact_prefix)
        is_history = entry.name.startswith(history_prefix)
        if phase is None and not is_temporary and not is_artifact_root and not is_history:
            continue
        if _is_link_or_reparse_path(entry):
            raise LockError(f"coordinator publish path is indirect: {entry}")
        try:
            info = entry.lstat()
        except OSError as exc:
            raise LockError(f"cannot inspect coordinator publish path {entry}: {exc}") from exc
        expected_directory = phase is None and is_artifact_root
        if expected_directory:
            if not stat.S_ISDIR(info.st_mode):
                raise LockError(
                    f"coordinator artifact path is not a directory: {entry}"
                )
        elif not stat.S_ISREG(info.st_mode):
            raise LockError(f"coordinator journal path is not a regular file: {entry}")
        found.append(
            {
                "phase": (
                    phase
                    if phase is not None
                    else "artifact-residue"
                    if is_artifact_root
                    else "history"
                    if is_history
                    else "temporary"
                ),
                "name": entry.name,
            }
        )

    if not found:
        return None
    found.sort(key=lambda item: (item["phase"], item["name"]))
    blocking = [
        entry
        for entry in found
        if entry["phase"] != "history"
    ]
    if not blocking:
        return None
    return {"required": True, "entries": blocking}


@contextmanager
def project_sync_lock(
    project_root: PathLike,
    *,
    timeout: float | None = 10.0,
    poll_interval: float = 0.05,
) -> Iterator[ExclusiveFileLock]:
    """Acquire the canonical root lock used by every project_docs writer."""

    with exclusive_lock(
        project_sync_lock_path(project_root),
        timeout=timeout,
        poll_interval=poll_interval,
    ) as lock:
        yield lock


def read_utf8(path: PathLike) -> str:
    """Read a text file as strict UTF-8; malformed input is never substituted."""

    file_path = Path(path)
    try:
        return file_path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictUtf8Error(f"invalid UTF-8 in {file_path}: {exc}") from exc


def _read_utf8_bytes(path: PathLike) -> tuple[bytes, str]:
    file_path = Path(path)
    raw = file_path.read_bytes()
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictUtf8Error(f"invalid UTF-8 in {file_path}: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 hex digest for bytes."""

    return hashlib.sha256(data).hexdigest()


def sha256_file(path: PathLike, *, chunk_size: int = 1024 * 1024) -> str:
    """Stream a file and return its lowercase SHA-256 digest."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        while True:
            chunk = file_obj.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _reject_json_constant(value: str) -> Any:
    raise JsonContractError(f"non-standard JSON constant is forbidden: {value}")


def _parse_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise JsonContractError(f"non-finite JSON number is forbidden: {value}")
    return parsed


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _validate_json_unicode(value: Any, path: str = "$") -> None:
    if isinstance(value, str):
        if _contains_surrogate(value):
            raise JsonContractError(
                f"isolated Unicode surrogate is forbidden at {path}"
            )
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_unicode(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _contains_surrogate(key):
                raise JsonContractError(
                    f"isolated Unicode surrogate is forbidden in object key at {path}"
                )
            _validate_json_unicode(item, f"{path}[{key!r}]")


def _unique_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise JsonContractError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def parse_json_strict(text: str, *, source: str = "<memory>") -> Any:
    """Parse strict JSON text, rejecting duplicates, NaN, and surrogates."""

    if not isinstance(text, str):
        raise JsonContractError("strict JSON input must be text")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
        _validate_json_unicode(value)
        return value
    except JsonContractError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise JsonContractError(f"invalid JSON in {source}: {exc}") from exc


def read_json_strict(path: PathLike) -> Any:
    """Read strict UTF-8 JSON, rejecting duplicate keys and NaN/Infinity."""

    file_path = Path(path)
    text = read_utf8(file_path)
    return parse_json_strict(text, source=str(file_path))


def stable_json_dumps(value: Any) -> str:
    """Encode deterministic UTF-8 JSON with sorted keys and a final newline."""

    try:
        rendered = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
        rendered.encode("utf-8")
        return rendered
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise JsonContractError(
            f"value cannot be encoded as strict JSON: {exc}"
        ) from exc


def configure_stdio_utf8() -> None:
    """Configure both process text streams for strict UTF-8 when supported."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="strict")
            except (AttributeError, OSError, ValueError):
                pass


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return rendered.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise JsonContractError(f"value cannot be canonically encoded: {exc}") from exc


def _fsync_parent_directory(parent: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_bytes(path: PathLike, payload: bytes) -> Path:
    destination = Path(path)
    parent = destination.parent
    if not parent.is_dir():
        raise AtomicWriteError(f"destination parent does not exist: {parent}")

    fd = -1
    temporary: Path | None = None
    replaced = False
    try:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=parent,
        )
        temporary = Path(temp_name)
        with os.fdopen(fd, "wb") as file_obj:
            fd = -1
            file_obj.write(payload)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary, destination)
        replaced = True
        _fsync_parent_directory(parent)
        return destination
    except OSError as exc:
        raise AtomicWriteError(f"atomic write failed for {destination}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if not replaced and temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def atomic_write_utf8(path: PathLike, text: str) -> Path:
    """Atomically replace a text file using strict UTF-8 bytes."""

    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrictUtf8Error(f"text cannot be encoded as UTF-8: {exc}") from exc
    return _atomic_write_bytes(path, payload)


def atomic_write_json(path: PathLike, value: Any) -> Path:
    """Atomically write stable JSON through a unique same-directory temp file."""

    return atomic_write_utf8(path, stable_json_dumps(value))


def _validate_portable_path_segment(segment: str, path: str) -> None:
    if any(
        ord(character) < 32 or character in _WINDOWS_FORBIDDEN_CHARS
        for character in segment
    ):
        raise PathValidationError(
            f"document path contains a non-portable filename character: {path!r}"
        )
    if segment.endswith((" ", ".")):
        raise PathValidationError(
            f"document path segment ends with a space or dot: {path!r}"
        )
    device_stem = segment.split(".", 1)[0]
    if _WINDOWS_RESERVED_NAME_RE.fullmatch(device_stem):
        raise PathValidationError(
            f"document path uses a reserved Windows device name: {path!r}"
        )


def validate_manifest_relative_path(
    relative_path: str | PurePosixPath,
) -> PurePosixPath:
    """Validate the lexical form of one portable manifest document path."""

    raw = (
        relative_path.as_posix()
        if isinstance(relative_path, PurePosixPath)
        else relative_path
    )
    if not isinstance(raw, str):
        raise PathValidationError("manifest path must be a string or PurePosixPath")
    if not raw:
        raise PathValidationError("manifest path must not be empty")
    if "\0" in raw:
        raise PathValidationError("manifest path contains NUL")
    if "\\" in raw:
        raise PathValidationError("manifest path must use POSIX '/' separators")
    if raw.startswith("//") or PureWindowsPath(raw).is_absolute():
        raise PathValidationError(f"UNC manifest path is forbidden: {raw!r}")
    if raw.startswith("/"):
        raise PathValidationError(f"absolute manifest path is forbidden: {raw!r}")
    if _WINDOWS_DRIVE_RE.match(raw) or PureWindowsPath(raw).drive:
        raise PathValidationError(
            f"drive-qualified manifest path is forbidden: {raw!r}"
        )

    segments = raw.split("/")
    if any(segment == "" for segment in segments):
        raise PathValidationError(f"manifest path contains an empty segment: {raw!r}")
    if any(segment in (".", "..") for segment in segments):
        raise PathValidationError(f"manifest path contains '.' or '..': {raw!r}")
    for segment in segments:
        _validate_portable_path_segment(segment, raw)

    pure_path = PurePosixPath(raw)
    if pure_path.is_absolute():
        raise PathValidationError(f"absolute manifest path is forbidden: {raw!r}")
    if pure_path.suffix != ".md" or pure_path.name == ".md":
        raise PathValidationError(
            f"manifest path must end in a named '.md' file: {raw!r}"
        )
    if pure_path.as_posix() != raw:
        raise PathValidationError(f"manifest path is not canonical POSIX form: {raw!r}")
    return pure_path


def manifest_path_depth(relative_path: str | PurePosixPath) -> int:
    """Derive manifest depth from POSIX parts (a root-level document is 0)."""

    pure_path = validate_manifest_relative_path(relative_path)
    return len(pure_path.parts) - 1


def _resolved_docs_root(docs_root: PathLike) -> Path:
    root = Path(docs_root)
    try:
        if _is_directory_link_or_junction(root):
            raise PathValidationError(
                f"project_docs root must not be a symlink or reparse point: {root}"
            )
        resolved = root.resolve(strict=True)
    except PathValidationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(f"project_docs root does not exist: {root}") from exc
    if not resolved.is_dir():
        raise PathValidationError(f"project_docs root is not a directory: {root}")
    return resolved


def resolve_docs_root(docs_root: PathLike) -> Path:
    """验证并返回真实 ``project_docs`` 根，拒绝根级间接路径。"""

    return _resolved_docs_root(docs_root)


def _ensure_below_root(candidate: Path, root: Path, description: str) -> None:
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PathValidationError(
            f"{description} resolves outside project_docs root: {candidate}"
        ) from exc


def resolve_manifest_document(
    docs_root: PathLike,
    relative_path: str | PurePosixPath,
    *,
    must_exist: bool = True,
) -> SafeManifestPath:
    """Resolve a manifest path and reject symlink/junction escapes."""

    root = _resolved_docs_root(docs_root)
    pure_path = validate_manifest_relative_path(relative_path)
    lexical = root.joinpath(*pure_path.parts)
    try:
        resolved = lexical.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(
            f"manifest document cannot be safely resolved: {pure_path.as_posix()}"
        ) from exc
    _ensure_below_root(resolved, root, f"manifest path {pure_path.as_posix()!r}")
    if must_exist and not resolved.is_file():
        raise PathValidationError(
            f"manifest path is not a regular document file: {pure_path.as_posix()}"
        )
    return SafeManifestPath(
        relative_path=pure_path.as_posix(),
        pure_path=pure_path,
        resolved_path=resolved,
        depth=len(pure_path.parts) - 1,
    )


def _read_all_fd(fd: int, *, chunk_size: int = 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, chunk_size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_posix_document_fd(root: Path, pure_path: PurePosixPath) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | no_follow
    opened_fds: list[int] = []
    try:
        current_fd = os.open(root, directory_flags)
        opened_fds.append(current_fd)
        for segment in pure_path.parts[:-1]:
            next_fd = os.open(
                segment,
                directory_flags,
                dir_fd=current_fd,
            )
            if not stat.S_ISDIR(os.fstat(next_fd).st_mode):
                os.close(next_fd)
                raise PathValidationError(
                    f"document parent is not a directory: {pure_path.as_posix()}"
                )
            opened_fds.append(next_fd)
            current_fd = next_fd

        file_fd = os.open(
            pure_path.name,
            os.O_RDONLY | no_follow,
            dir_fd=current_fd,
        )
        opened_fds.append(file_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise PathValidationError(
                f"document is not a regular file: {pure_path.as_posix()}"
            )
        return _read_all_fd(file_fd)
    finally:
        for fd in reversed(opened_fds):
            os.close(fd)


def _is_reparse_point(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _is_directory_link_or_junction(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return bool(
        path.is_symlink()
        or _is_reparse_point(path)
        or (junction_check is not None and junction_check())
    )


def _read_checked_document_fd(root: Path, pure_path: PurePosixPath) -> bytes:
    lexical = root.joinpath(*pure_path.parts)
    current = root
    for index, segment in enumerate(pure_path.parts):
        current = current / segment
        current_stat = current.lstat()
        if current.is_symlink() or _is_reparse_point(current):
            raise PathValidationError(
                f"document path contains a symlink or reparse point: {pure_path.as_posix()}"
            )
        if index < len(pure_path.parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            raise PathValidationError(
                f"document parent is not a directory: {pure_path.as_posix()}"
            )

    before = lexical.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise PathValidationError(
            f"document is not a regular file: {pure_path.as_posix()}"
        )

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(lexical, flags)
    try:
        opened = os.fstat(fd)
        after = lexical.lstat()
        before_identity = (before.st_dev, before.st_ino)
        if before_identity != (opened.st_dev, opened.st_ino) or before_identity != (
            after.st_dev,
            after.st_ino,
        ):
            raise PathValidationError(
                f"document changed while it was being opened: {pure_path.as_posix()}"
            )
        if _is_reparse_point(lexical) or not stat.S_ISREG(opened.st_mode):
            raise PathValidationError(
                f"document is not a stable regular file: {pure_path.as_posix()}"
            )
        return _read_all_fd(fd)
    finally:
        os.close(fd)


def _read_project_document_bytes(
    docs_root: PathLike,
    relative_path: str | PurePosixPath,
) -> tuple[SafeManifestPath, bytes, str]:
    safe_path = resolve_manifest_document(docs_root, relative_path, must_exist=True)
    root = _resolved_docs_root(docs_root)
    try:
        if os.name == "posix":
            raw = _read_posix_document_fd(root, safe_path.pure_path)
        else:
            raw = _read_checked_document_fd(root, safe_path.pure_path)
    except PathValidationError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PathValidationError(
            f"document cannot be securely opened: {safe_path.relative_path}"
        ) from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictUtf8Error(
            f"invalid UTF-8 in {safe_path.resolved_path}: {exc}"
        ) from exc
    return safe_path, raw, content


def read_project_document(
    docs_root: PathLike,
    relative_path: str | PurePosixPath,
) -> str:
    """Safely read one in-root regular document as strict UTF-8."""

    _, _, content = _read_project_document_bytes(docs_root, relative_path)
    return content


def ensure_unique_manifest_paths(
    relative_paths: Iterable[str | PurePosixPath],
) -> list[PurePosixPath]:
    """Validate paths and reject exact duplicates and case-only conflicts."""

    seen: dict[str, str] = {}
    validated: list[PurePosixPath] = []
    for value in relative_paths:
        pure_path = validate_manifest_relative_path(value)
        canonical = pure_path.as_posix()
        folded = canonical.casefold()
        previous = seen.get(folded)
        if previous is not None:
            kind = "duplicate" if previous == canonical else "case conflict"
            raise PathConflictError(
                f"manifest path {kind}: {previous!r} conflicts with {canonical!r}"
            )
        seen[folded] = canonical
        validated.append(pure_path)
    return validated


def validate_manifest_paths(
    docs_root: PathLike,
    relative_paths: Iterable[str | PurePosixPath],
    *,
    must_exist: bool = True,
) -> list[SafeManifestPath]:
    """Validate uniqueness and safely resolve a collection of manifest paths."""

    paths = ensure_unique_manifest_paths(relative_paths)
    return [
        resolve_manifest_document(docs_root, path, must_exist=must_exist)
        for path in paths
    ]


def _source_document_path(
    source_doc: PathLike, docs_root: PathLike
) -> tuple[Path, Path]:
    """返回源码文档的逻辑路径，不跟随最后一刻出现的间接路径。"""

    root = _resolved_docs_root(docs_root)
    raw = os.fspath(source_doc)
    if not isinstance(raw, str):
        raise LinkResolutionError("source document path must be text")
    if "\0" in raw:
        raise LinkResolutionError("source document path contains NUL")
    if isinstance(source_doc, str) and "\\" in raw:
        raise LinkResolutionError(
            "text source document paths must use POSIX separators"
        )

    source = Path(raw)
    try:
        if source.is_absolute():
            lexical = Path(os.path.abspath(raw))
            relative = lexical.relative_to(root).as_posix()
            validate_manifest_relative_path(relative)
        else:
            pure_path = validate_manifest_relative_path(raw)
            lexical = root.joinpath(*pure_path.parts)
    except (OSError, RuntimeError, ValueError, PathValidationError) as exc:
        raise LinkResolutionError(
            f"source document is outside or invalid for project_docs root: {source_doc}"
        ) from exc

    try:
        if os.path.lexists(lexical) and _is_directory_link_or_junction(lexical):
            raise LinkResolutionError(
                f"source document is outside or uses an indirect path: {source_doc}"
            )
    except LinkResolutionError:
        raise
    except OSError as exc:
        raise LinkResolutionError(
            f"source document path cannot be inspected safely: {source_doc}"
        ) from exc
    return root, lexical


def _link_result(
    *,
    raw_target: str,
    source_path: Path,
    disposition: LinkDisposition,
    reason: str,
    normalized_target: str | None = None,
    project_relative_path: str | None = None,
    resolved_path: Path | None = None,
    exists: bool | None = None,
    query: str | None = None,
    fragment: str | None = None,
) -> MarkdownLinkResult:
    return MarkdownLinkResult(
        text="",
        raw_target=raw_target,
        disposition=disposition,
        source_path=source_path,
        normalized_target=normalized_target,
        project_relative_path=project_relative_path,
        resolved_path=resolved_path,
        exists=exists,
        reason=reason,
        query=query,
        fragment=fragment,
    )


def resolve_markdown_target(
    target: str,
    *,
    source_doc: PathLike,
    docs_root: PathLike,
) -> MarkdownLinkResult:
    """Classify and safely resolve one Markdown link target.

    Invalid local paths are represented as ``INVALID`` results rather than being
    silently normalized. Broken-but-safe document links remain ``LOCAL_DOCUMENT``
    with ``exists=False`` so callers can report them consistently.
    """

    root, source_path = _source_document_path(source_doc, docs_root)
    if not isinstance(target, str):
        return _link_result(
            raw_target=repr(target),
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="target-is-not-text",
        )

    raw_target = target
    cleaned = target.strip()
    if cleaned.startswith("<") or cleaned.endswith(">"):
        if not (cleaned.startswith("<") and cleaned.endswith(">")):
            return _link_result(
                raw_target=raw_target,
                source_path=source_path,
                disposition=LinkDisposition.INVALID,
                reason="malformed-angle-target",
            )
        cleaned = cleaned[1:-1].strip()

    if not cleaned:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="empty-target",
        )
    if cleaned.startswith("#"):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="same-document-anchor",
            fragment=cleaned[1:] or None,
        )
    if "\0" in cleaned or "\\" in cleaned:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="forbidden-path-character",
        )
    if _WINDOWS_DRIVE_RE.match(cleaned):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="drive-qualified-target",
        )
    if cleaned.startswith("//"):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="network-url",
        )
    if _URI_SCHEME_RE.match(cleaned):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="external-uri-scheme",
        )

    path_and_query, fragment_separator, fragment = cleaned.partition("#")
    path_text, query_separator, query = path_and_query.partition("?")
    fragment_value = fragment if fragment_separator else None
    query_value = query if query_separator else None
    if not path_text:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="empty-local-path",
            query=query_value,
            fragment=fragment_value,
        )
    if _INVALID_PERCENT_RE.search(path_text):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="malformed-percent-escape",
            query=query_value,
            fragment=fragment_value,
        )

    try:
        decoded = unquote(path_text, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="invalid-utf8-percent-escape",
            query=query_value,
            fragment=fragment_value,
        )

    if "\0" in decoded or "\\" in decoded:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="forbidden-decoded-character",
            query=query_value,
            fragment=fragment_value,
        )
    if _WINDOWS_DRIVE_RE.match(decoded):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="drive-qualified-target",
            query=query_value,
            fragment=fragment_value,
        )
    if _URI_SCHEME_RE.match(decoded):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="external-uri-scheme",
            query=query_value,
            fragment=fragment_value,
        )

    pure_target = PurePosixPath(decoded)
    if pure_target.is_absolute():
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="absolute-local-target",
            query=query_value,
            fragment=fragment_value,
        )
    decoded_segments = decoded.split("/")
    if any(segment == "" for segment in decoded_segments):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="empty-local-path-segment",
            query=query_value,
            fragment=fragment_value,
        )
    try:
        for segment in decoded_segments:
            if segment not in (".", ".."):
                _validate_portable_path_segment(segment, decoded)
    except PathValidationError:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="non-portable-local-target",
            query=query_value,
            fragment=fragment_value,
        )

    suffix = pure_target.suffix.casefold()
    if suffix in SOURCE_FILE_EXTENSIONS:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="source-file-reference",
            normalized_target=pure_target.as_posix(),
            query=query_value,
            fragment=fragment_value,
        )
    if not suffix:
        pure_target = PurePosixPath(pure_target.as_posix() + ".md")
    elif suffix != ".md" or pure_target.suffix != ".md":
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.SKIPPED,
            reason="non-markdown-reference",
            normalized_target=pure_target.as_posix(),
            query=query_value,
            fragment=fragment_value,
        )

    try:
        resolved = source_path.parent.joinpath(*pure_target.parts).resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="target-outside-docs-root",
            normalized_target=pure_target.as_posix(),
            query=query_value,
            fragment=fragment_value,
        )

    relative = resolved.relative_to(root).as_posix()
    try:
        validate_manifest_relative_path(relative)
    except PathValidationError:
        return _link_result(
            raw_target=raw_target,
            source_path=source_path,
            disposition=LinkDisposition.INVALID,
            reason="noncanonical-document-target",
            normalized_target=pure_target.as_posix(),
            query=query_value,
            fragment=fragment_value,
        )

    return _link_result(
        raw_target=raw_target,
        source_path=source_path,
        disposition=LinkDisposition.LOCAL_DOCUMENT,
        reason="resolved-document" if resolved.is_file() else "missing-document",
        normalized_target=pure_target.as_posix(),
        project_relative_path=relative,
        resolved_path=resolved,
        exists=resolved.is_file(),
        query=query_value,
        fragment=fragment_value,
    )


def _reference_label(value: str) -> str:
    return " ".join(value.split()).casefold()


def _span_overlaps(span: tuple[int, int], occupied: Sequence[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in occupied)


def parse_markdown_links(
    content: str,
    *,
    source_doc: PathLike,
    docs_root: PathLike,
) -> list[MarkdownLinkResult]:
    """Parse inline, reference-style, and HTML-anchor document links.

    Reference labels follow CommonMark's whitespace folding and case-insensitive
    lookup.  The first definition wins, matching CommonMark behavior.
    """

    results: list[MarkdownLinkResult] = []
    occupied: list[tuple[int, int]] = []
    definitions: dict[str, str] = {}
    definition_spans: list[tuple[int, int]] = []
    for match in _MARKDOWN_REFERENCE_DEFINITION_RE.finditer(content):
        label = _reference_label(match.group(1))
        if label and label not in definitions:
            definitions[label] = match.group(2)
        definition_spans.append(match.span())

    for match in _MARKDOWN_LINK_RE.finditer(content):
        result = resolve_markdown_target(
            match.group(2),
            source_doc=source_doc,
            docs_root=docs_root,
        )
        results.append(
            replace(
                result,
                text=match.group(1),
                span=match.span(),
            )
        )
        occupied.append(match.span())

    for match in _MARKDOWN_REFERENCE_LINK_RE.finditer(content):
        if _span_overlaps(match.span(), occupied) or _span_overlaps(
            match.span(), definition_spans
        ):
            continue
        raw_label = match.group(2) or match.group(1)
        target = definitions.get(_reference_label(raw_label))
        if target is None:
            continue
        result = resolve_markdown_target(
            target,
            source_doc=source_doc,
            docs_root=docs_root,
        )
        results.append(replace(result, text=match.group(1), span=match.span()))
        occupied.append(match.span())

    for match in _MARKDOWN_SHORTCUT_REFERENCE_RE.finditer(content):
        if _span_overlaps(match.span(), occupied) or _span_overlaps(
            match.span(), definition_spans
        ):
            continue
        target = definitions.get(_reference_label(match.group(1)))
        if target is None:
            continue
        result = resolve_markdown_target(
            target,
            source_doc=source_doc,
            docs_root=docs_root,
        )
        results.append(replace(result, text=match.group(1), span=match.span()))
        occupied.append(match.span())

    for match in _HTML_HREF_RE.finditer(content):
        if _span_overlaps(match.span(), occupied):
            continue
        target = match.group(1) if match.group(1) is not None else match.group(2)
        result = resolve_markdown_target(
            target,
            source_doc=source_doc,
            docs_root=docs_root,
        )
        results.append(replace(result, text="", span=match.span()))

    results.sort(key=lambda result: result.span or (0, 0))
    return results


def read_markdown_links(
    source_doc: PathLike,
    docs_root: PathLike,
) -> list[MarkdownLinkResult]:
    """Contain, securely open, and parse one strict UTF-8 document."""

    root, lexical_source = _source_document_path(source_doc, docs_root)
    relative_source = lexical_source.relative_to(root).as_posix()
    _, _, content = _read_project_document_bytes(root, relative_source)
    return parse_markdown_links(
        content,
        source_doc=lexical_source,
        docs_root=root,
    )


def iter_local_document_links(
    links: Iterable[MarkdownLinkResult],
) -> Iterator[MarkdownLinkResult]:
    """Yield only safely resolved local document links."""

    for link in links:
        if link.is_local_document:
            yield link


def _logical_relative_document(
    path: PathLike,
    root: Path,
    *,
    must_exist: bool,
) -> str:
    raw = os.fspath(path)
    if not isinstance(raw, str):
        raise PathValidationError("document path must be text")
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            resolved = candidate.resolve(strict=must_exist)
            relative = resolved.relative_to(root).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise PathValidationError(
                f"document is outside project_docs: {path}"
            ) from exc
    else:
        pure_path = validate_manifest_relative_path(raw)
        relative = resolve_manifest_document(
            root,
            pure_path,
            must_exist=must_exist,
        ).relative_path
    validate_manifest_relative_path(relative)
    return relative


def compute_relative_doc_link(
    from_doc: PathLike,
    to_doc: PathLike,
    docs_root: PathLike,
    *,
    must_exist: bool = False,
) -> str:
    """Compute a safe POSIX link, including for documents not written yet."""

    root = _resolved_docs_root(docs_root)
    source = PurePosixPath(
        _logical_relative_document(from_doc, root, must_exist=must_exist)
    )
    target = _logical_relative_document(to_doc, root, must_exist=must_exist)
    source_parent = source.parent.as_posix()
    return posixpath.relpath(target, source_parent if source_parent != "." else ".")


def parse_last_synced_marker(content: str) -> str | None:
    """Extract the timestamp from the first complete Last synced marker.

    The non-greedy marker match deliberately stops at the first ``-->`` so a
    historical duplicated suffix after the marker cannot contaminate the value.
    """

    match = _LAST_SYNCED_RE.search(content)
    if match is None:
        return None
    first_segment = re.split(r"\s+by\s+", match.group(1).strip(), maxsplit=1)[0].strip()
    return first_segment or None


def parse_document_metadata(
    content: str,
    relative_path: str | PurePosixPath,
    *,
    fallback_synced_at: str | None = None,
) -> DocumentMetadata:
    """Parse first H1 title, status section, and first Last synced marker."""

    pure_path = validate_manifest_relative_path(relative_path)
    title_match = _TITLE_RE.search(content.lstrip("﻿"))
    title = (
        title_match.group(1).strip()
        if title_match is not None
        else pure_path.with_suffix("").as_posix()
    )

    status = "❓"
    status_match = _STATUS_SECTION_RE.search(content)
    if status_match is not None:
        body = status_match.group(1)
        for marker in _STATUS_MARKERS:
            if marker in body:
                status = marker
                break

    synced_at = parse_last_synced_marker(content) or fallback_synced_at
    return DocumentMetadata(title=title, status=status, synced_at=synced_at)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{field} must be a non-empty ISO-8601 string")
    normalized = value.strip()
    candidate = normalized[:-1] + "+00:00" if normalized.endswith("Z") else normalized
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ManifestValidationError(
            f"{field} is not valid ISO-8601: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestValidationError(f"{field} must include a timezone: {value!r}")
    return normalized


def _timestamp_or_none(value: Any) -> str | None:
    try:
        return _parse_timestamp(value, "timestamp")
    except ManifestValidationError:
        return None


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()


def _depth_counts(files: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in files:
        key = str(entry["depth"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: int(item[0])))


def compute_generation_id(files: Sequence[Mapping[str, Any]]) -> str:
    """Hash canonical, non-volatile document content metadata."""

    fields = ("path", "depth", "title", "status", "bytes", "sha256")
    canonical_files: list[dict[str, Any]] = []
    for index, entry in enumerate(files):
        missing = [field for field in fields if field not in entry]
        if missing:
            raise ManifestValidationError(
                f"files[{index}] lacks generation fields: {', '.join(missing)}"
            )
        canonical_files.append({field: entry[field] for field in fields})
    canonical_files.sort(key=lambda entry: entry["path"])
    return sha256_bytes(_canonical_json_bytes({"files": canonical_files}))


def discover_active_markdown_paths(docs_root: PathLike) -> list[str]:
    """Return the complete active Markdown set without following indirections."""

    root = _resolved_docs_root(docs_root)
    relative_paths: list[str] = []

    def visit(directory: Path) -> None:
        try:
            before = directory.lstat()
            if _is_directory_link_or_junction(directory) or not stat.S_ISDIR(
                before.st_mode
            ):
                raise PathValidationError(
                    f"active document directory is indirect or invalid: {directory}"
                )
            with os.scandir(directory) as iterator:
                entries = sorted(
                    iterator,
                    key=lambda item: (item.name.casefold(), item.name),
                )
            after = directory.lstat()
        except PathValidationError:
            raise
        except OSError as exc:
            raise PathValidationError(
                f"cannot securely enumerate active documents: {directory}"
            ) from exc

        if _is_directory_link_or_junction(directory) or not _same_file_identity(
            before,
            after,
        ):
            raise PathValidationError(
                f"active document directory changed while scanning: {directory}"
            )

        for item in entries:
            path = Path(item.path)
            try:
                item_stat = path.lstat()
                indirect = _is_directory_link_or_junction(path)
            except OSError as exc:
                raise PathValidationError(
                    f"active document path changed while scanning: {path}"
                ) from exc
            if indirect:
                raise PathValidationError(
                    f"active document tree contains a symlink or reparse point: {path}"
                )
            if stat.S_ISDIR(item_stat.st_mode):
                is_root_quarantine = (
                    directory == root
                    and item.name.casefold() == QUARANTINE_DIRNAME.casefold()
                )
                if is_root_quarantine and item.name != QUARANTINE_DIRNAME:
                    raise PathValidationError(
                        "root quarantine directory must use canonical lowercase name: "
                        f"{path}"
                    )
                if not is_root_quarantine:
                    visit(path)
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                continue
            if path.suffix.casefold() == ".md" and path.suffix != ".md":
                raise PathValidationError(
                    f"Markdown extension must use canonical lowercase '.md': {path}"
                )
            if path.suffix == ".md":
                relative_paths.append(path.relative_to(root).as_posix())

    visit(root)
    ensure_unique_manifest_paths(relative_paths)
    return sorted(relative_paths)


def _validate_manifest_document_set(root: Path, paths: Sequence[str]) -> None:
    """Require a verified manifest to cover the complete active Markdown set."""

    active_paths = discover_active_markdown_paths(root)
    manifest_paths = set(paths)
    active_set = set(active_paths)
    if manifest_paths == active_set:
        return

    manifest_by_folded = {path.casefold(): path for path in manifest_paths}
    active_by_folded = {path.casefold(): path for path in active_set}
    case_mismatches = sorted(
        f"{manifest_by_folded[key]!r} != {active_by_folded[key]!r}"
        for key in manifest_by_folded.keys() & active_by_folded.keys()
        if manifest_by_folded[key] != active_by_folded[key]
    )
    unregistered = sorted(active_set - manifest_paths)
    missing = sorted(manifest_paths - active_set)
    details: list[str] = []
    if unregistered:
        details.append(f"unregistered active documents={unregistered!r}")
    if missing:
        details.append(f"missing registered documents={missing!r}")
    if case_mismatches:
        details.append(f"case mismatches={case_mismatches!r}")
    raise ManifestFileMismatchError("document set mismatch: " + "; ".join(details))


def _document_record(
    root: Path,
    relative_path: str,
    *,
    fallback_synced_at: str,
) -> dict[str, Any]:
    safe_path, raw, content = _read_project_document_bytes(root, relative_path)
    metadata = parse_document_metadata(
        content,
        relative_path,
        fallback_synced_at=fallback_synced_at,
    )
    synced_at = _parse_timestamp(
        metadata.synced_at,
        f"file {relative_path!r} synced_at",
    )
    return {
        "path": relative_path,
        "depth": safe_path.depth,
        "title": metadata.title,
        "status": metadata.status,
        "bytes": len(raw),
        "sha256": sha256_bytes(raw),
        "synced_at": synced_at,
    }


def build_manifest(
    docs_root: PathLike,
    *,
    producer: Mapping[str, str] | None = None,
    last_synced: str | None = None,
    docs_root_label: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic-order schema-v2 manifest from strict UTF-8 docs."""

    root = _resolved_docs_root(docs_root)
    producer_value = dict(
        producer
        or {
            "name": DEFAULT_PRODUCER_NAME,
            "version": DEFAULT_PRODUCER_VERSION,
        }
    )
    for field in ("name", "version"):
        if not isinstance(producer_value.get(field), str) or not producer_value[field]:
            raise ManifestValidationError(
                f"producer.{field} must be a non-empty string"
            )

    synced_at = _parse_timestamp(last_synced or _utc_now_iso(), "last_synced")
    label = docs_root_label if docs_root_label is not None else root.name
    if not isinstance(label, str) or not label or "\0" in label:
        raise ManifestValidationError(
            "docs_root label must be a non-empty NUL-free string"
        )

    files = [
        _document_record(root, relative_path, fallback_synced_at=synced_at)
        for relative_path in discover_active_markdown_paths(root)
    ]
    files.sort(key=lambda entry: entry["path"])
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "producer": producer_value,
        "generation_id": compute_generation_id(files),
        "docs_root": label,
        "files": files,
        "total": len(files),
        "depth_counts": _depth_counts(files),
        "last_synced": synced_at,
    }
    return validate_manifest_v2(manifest, root, verify_files=True)


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestSchemaError(f"{field} must be an object")
    return value


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestValidationError(f"{field} must be a non-empty string")
    return value


def _require_integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ManifestValidationError(f"{field} must be an integer >= {minimum}")
    return value


def _validate_depth_counts(value: Any, expected: Mapping[str, int]) -> None:
    mapping = _require_mapping(value, "depth_counts")
    normalized: dict[str, int] = {}
    for key, count in mapping.items():
        if not isinstance(key, str) or not re.fullmatch(r"0|[1-9][0-9]*", key):
            raise ManifestValidationError(
                "depth_counts keys must be canonical non-negative decimal strings"
            )
        normalized[key] = _require_integer(count, f"depth_counts[{key!r}]", minimum=1)
    if normalized != dict(expected):
        raise ManifestValidationError(
            f"depth_counts mismatch: expected {dict(expected)!r}, got {normalized!r}"
        )


def _verify_single_file_v2(
    path: str,
    byte_count: int,
    digest: str,
    synced_at: str,
    title: str,
    status: str,
    root: Path,
    manifest_synced_at: str,
) -> None:
    safe_path, raw, content = _read_project_document_bytes(root, path)
    actual_digest = sha256_bytes(raw)
    if byte_count != len(raw):
        raise ManifestFileMismatchError(
            f"file size mismatch for {path!r}: manifest={byte_count}, actual={len(raw)}"
        )
    if digest != actual_digest:
        raise ManifestFileMismatchError(
            f"SHA-256 mismatch for {path!r}: manifest={digest}, actual={actual_digest}"
        )
    metadata = parse_document_metadata(
        content,
        path,
        fallback_synced_at=manifest_synced_at,
    )
    expected_synced_at = _parse_timestamp(
        metadata.synced_at,
        f"document {path!r} synced_at",
    )
    metadata_pairs = (
        ("title", title, metadata.title),
        ("status", status, metadata.status),
        ("synced_at", synced_at, expected_synced_at),
    )
    for field, manifest_value, document_value in metadata_pairs:
        if manifest_value != document_value:
            raise ManifestFileMismatchError(
                f"{field} mismatch for {path!r}: "
                f"manifest={manifest_value!r}, document={document_value!r}"
            )


def validate_manifest_v2(
    manifest: Mapping[str, Any],
    docs_root: PathLike,
    *,
    verify_files: bool = True,
    verify_complete_set: bool = True,
) -> dict[str, Any]:
    """Strictly validate a schema-v2 manifest and return an isolated copy."""

    data = copy.deepcopy(dict(_require_mapping(manifest, "manifest")))
    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestSchemaError(
            f"expected schema_version={MANIFEST_SCHEMA_VERSION}, got {schema_version!r}"
        )

    producer = _require_mapping(data.get("producer"), "producer")
    _require_nonempty_string(producer.get("name"), "producer.name")
    _require_nonempty_string(producer.get("version"), "producer.version")
    _require_nonempty_string(data.get("docs_root"), "docs_root")
    manifest_synced_at = _parse_timestamp(data.get("last_synced"), "last_synced")

    generation_id = _require_nonempty_string(data.get("generation_id"), "generation_id")
    if _SHA256_RE.fullmatch(generation_id) is None:
        raise ManifestValidationError(
            "generation_id must be a lowercase SHA-256 digest"
        )

    files_value = data.get("files")
    if not isinstance(files_value, list):
        raise ManifestSchemaError("files must be an array")
    files: list[dict[str, Any]] = []
    paths: list[str] = []
    root = _resolved_docs_root(docs_root)

    verify_tasks: list[tuple[str, int, str, str, str, str, Path, str]] = []
    for index, raw_entry in enumerate(files_value):
        entry = dict(_require_mapping(raw_entry, f"files[{index}]"))
        path = _require_nonempty_string(entry.get("path"), f"files[{index}].path")
        pure_path = validate_manifest_relative_path(path)
        paths.append(path)

        depth = _require_integer(entry.get("depth"), f"files[{index}].depth")
        expected_depth = len(pure_path.parts) - 1
        if depth != expected_depth:
            raise ManifestValidationError(
                f"files[{index}].depth is {depth}, expected {expected_depth} from {path!r}"
            )
        title = _require_nonempty_string(entry.get("title"), f"files[{index}].title")
        status = _require_nonempty_string(entry.get("status"), f"files[{index}].status")
        if status not in _STATUS_MARKERS:
            raise ManifestValidationError(
                f"files[{index}].status must be one of {_STATUS_MARKERS!r}"
            )
        byte_count = _require_integer(entry.get("bytes"), f"files[{index}].bytes")
        digest = _require_nonempty_string(entry.get("sha256"), f"files[{index}].sha256")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ManifestValidationError(
                f"files[{index}].sha256 must be a lowercase SHA-256 digest"
            )
        synced_at = _parse_timestamp(
            entry.get("synced_at"),
            f"files[{index}].synced_at",
        )

        if verify_files:
            verify_tasks.append(
                (path, byte_count, digest, synced_at, title, status, root, manifest_synced_at)
            )
        else:
            resolve_manifest_document(root, path, must_exist=False)
        files.append(entry)

    if verify_files and verify_tasks:
        if len(verify_tasks) > 1:
            max_workers = min(32, (os.cpu_count() or 4) * 4)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(lambda t: _verify_single_file_v2(*t), verify_tasks))
        else:
            for task in verify_tasks:
                _verify_single_file_v2(*task)

    ensure_unique_manifest_paths(paths)
    if verify_files and verify_complete_set:
        _validate_manifest_document_set(root, paths)
    expected_order = sorted(paths)
    if paths != expected_order:
        raise ManifestValidationError("files must be sorted by path")

    total = _require_integer(data.get("total"), "total")
    if total != len(files):
        raise ManifestValidationError(
            f"total mismatch: manifest={total}, files={len(files)}"
        )
    expected_depth_counts = _depth_counts(files)
    _validate_depth_counts(data.get("depth_counts"), expected_depth_counts)

    expected_generation = compute_generation_id(files)
    if generation_id != expected_generation:
        raise ManifestValidationError(
            f"generation_id mismatch: manifest={generation_id}, expected={expected_generation}"
        )
    return data


def _normalize_manifest_v1(
    manifest: Mapping[str, Any],
    docs_root: PathLike,
    *,
    verify_complete_set: bool = True,
) -> dict[str, Any]:
    data = copy.deepcopy(dict(_require_mapping(manifest, "manifest")))
    files_value = data.get("files")
    if not isinstance(files_value, list):
        raise ManifestSchemaError("v1 files must be an array")

    warnings.warn(
        "manifest v1 depth values are untrusted and were recomputed in memory; "
        "the source manifest was not rewritten",
        LegacyManifestWarning,
        stacklevel=3,
    )

    root = _resolved_docs_root(docs_root)
    top_synced_at = _timestamp_or_none(data.get("last_synced"))
    raw_paths: list[str] = []
    entries: list[dict[str, Any]] = []

    for index, raw_entry in enumerate(files_value):
        entry = dict(_require_mapping(raw_entry, f"files[{index}]"))
        path = _require_nonempty_string(entry.get("path"), f"files[{index}].path")
        raw_paths.append(path)
        safe_path, raw, content = _read_project_document_bytes(root, path)
        fallback = top_synced_at or _mtime_iso(safe_path.resolved_path)
        metadata = parse_document_metadata(content, path, fallback_synced_at=fallback)

        title = entry.get("title")
        status = entry.get("status")
        entry.update(
            {
                "path": path,
                "depth": safe_path.depth,
                "title": title if isinstance(title, str) and title else metadata.title,
                "status": (
                    status if isinstance(status, str) and status else metadata.status
                ),
                "bytes": len(raw),
                "sha256": sha256_bytes(raw),
                "synced_at": _parse_timestamp(
                    metadata.synced_at,
                    f"files[{index}].synced_at",
                ),
            }
        )
        entries.append(entry)

    ensure_unique_manifest_paths(raw_paths)
    if verify_complete_set:
        _validate_manifest_document_set(root, raw_paths)
    entries.sort(key=lambda entry: entry["path"])
    data["schema_version"] = 1
    data["files"] = entries
    data["total"] = len(entries)
    data["depth_counts"] = _depth_counts(entries)
    data.setdefault("docs_root", root.name)
    if top_synced_at is not None:
        data["last_synced"] = top_synced_at
    return data


def validate_manifest(
    manifest: Mapping[str, Any],
    docs_root: PathLike,
    *,
    verify_files: bool = True,
    verify_complete_set: bool = True,
) -> dict[str, Any]:
    """Validate v2 or normalize legacy v1 without writing it back."""

    mapping = _require_mapping(manifest, "manifest")
    if "schema_version" not in mapping:
        return _normalize_manifest_v1(
            mapping,
            docs_root,
            verify_complete_set=verify_complete_set,
        )

    schema_version = mapping.get("schema_version")
    if type(schema_version) is int and schema_version == 1:
        return _normalize_manifest_v1(
            mapping,
            docs_root,
            verify_complete_set=verify_complete_set,
        )
    if type(schema_version) is int and schema_version == MANIFEST_SCHEMA_VERSION:
        return validate_manifest_v2(
            mapping,
            docs_root,
            verify_files=verify_files,
            verify_complete_set=verify_complete_set,
        )
    raise ManifestSchemaError(
        f"unsupported manifest schema_version: {schema_version!r}"
    )


def _manifest_location(
    manifest_path_or_root: PathLike,
    docs_root: PathLike | None,
) -> tuple[Path, Path]:
    supplied = Path(manifest_path_or_root)
    if supplied.is_dir():
        manifest_path = supplied / MANIFEST_FILENAME
        root = supplied
    else:
        manifest_path = supplied
        root = Path(docs_root) if docs_root is not None else supplied.parent
    return manifest_path, root


def load_manifest(
    manifest_path_or_root: PathLike,
    *,
    docs_root: PathLike | None = None,
    verify_files: bool = True,
    verify_complete_set: bool = True,
) -> dict[str, Any]:
    """Strictly load and validate ``_manifest.json`` or a supplied JSON path."""

    manifest_path, root = _manifest_location(manifest_path_or_root, docs_root)
    value = read_json_strict(manifest_path)
    if not isinstance(value, Mapping):
        raise ManifestSchemaError("manifest JSON root must be an object")
    return validate_manifest(
        value,
        root,
        verify_files=verify_files,
        verify_complete_set=verify_complete_set,
    )


def write_manifest(
    manifest_path_or_root: PathLike,
    manifest: Mapping[str, Any],
    *,
    timeout: float | None = 10.0,
    use_lock: bool = True,
) -> Path:
    """Atomically write a manifest, serializing concurrent writers by default."""

    supplied = Path(manifest_path_or_root)
    manifest_path = supplied / MANIFEST_FILENAME if supplied.is_dir() else supplied
    if not use_lock:
        return atomic_write_json(manifest_path, dict(manifest))
    lock_path = manifest_path.with_name(manifest_path.name + ".lock")
    project_root = manifest_path.parent.resolve(strict=True).parent
    with project_sync_lock(project_root, timeout=timeout):
        with exclusive_lock(lock_path, timeout=timeout):
            return atomic_write_json(manifest_path, dict(manifest))


def build_and_write_manifest(
    docs_root: PathLike,
    *,
    manifest_path: PathLike | None = None,
    producer: Mapping[str, str] | None = None,
    last_synced: str | None = None,
    docs_root_label: str | None = None,
    timeout: float | None = 10.0,
) -> dict[str, Any]:
    """Lock the complete schema-v2 snapshot-and-replace transaction."""

    supplied = Path(manifest_path or Path(docs_root) / MANIFEST_FILENAME)
    destination = supplied / MANIFEST_FILENAME if supplied.is_dir() else supplied
    lock_path = destination.with_name(destination.name + ".lock")
    project_root = Path(docs_root).resolve(strict=True).parent
    with project_sync_lock(project_root, timeout=timeout):
        with exclusive_lock(lock_path, timeout=timeout):
            manifest = build_manifest(
                docs_root,
                producer=producer,
                last_synced=last_synced,
                docs_root_label=docs_root_label,
            )
            atomic_write_json(destination, manifest)
    return manifest


__all__ = [
    "AtomicWriteError",
    "DEFAULT_PRODUCER_NAME",
    "DEFAULT_PRODUCER_VERSION",
    "DocumentMetadata",
    "ExclusiveFileLock",
    "JsonContractError",
    "LegacyManifestWarning",
    "LinkDisposition",
    "LinkResolutionError",
    "LockError",
    "LockTimeoutError",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestError",
    "ManifestFileMismatchError",
    "ManifestSchemaError",
    "ManifestValidationError",
    "MarkdownLinkResult",
    "PathConflictError",
    "PathValidationError",
    "ProjectDocsContractError",
    "QUARANTINE_DIRNAME",
    "SOURCE_FILE_EXTENSIONS",
    "SafeManifestPath",
    "StrictUtf8Error",
    "atomic_write_json",
    "atomic_write_utf8",
    "build_and_write_manifest",
    "build_manifest",
    "compute_generation_id",
    "compute_relative_doc_link",
    "configure_stdio_utf8",
    "discover_active_markdown_paths",
    "ensure_unique_manifest_paths",
    "exclusive_file_lock",
    "exclusive_lock",
    "iter_local_document_links",
    "load_manifest",
    "manifest_path_depth",
    "parse_document_metadata",
    "parse_json_strict",
    "parse_last_synced_marker",
    "parse_markdown_links",
    "project_sync_lock",
    "project_sync_lock_path",
    "read_json_strict",
    "read_markdown_links",
    "read_project_document",
    "read_utf8",
    "resolve_docs_root",
    "resolve_manifest_document",
    "resolve_markdown_target",
    "sha256_bytes",
    "sha256_file",
    "stable_json_dumps",
    "validate_manifest",
    "validate_manifest_paths",
    "validate_manifest_relative_path",
    "validate_manifest_v2",
    "write_manifest",
]
