# -*- coding: utf-8 -*-
"""Focused tests for the shared project_docs contract."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path

import pytest

import project_docs_contract as contract

SYNCED_1 = "2026-07-29T12:00:00+08:00"
SYNCED_2 = "2026-07-30T12:00:00+08:00"


def _write_doc(
    path: Path,
    title: str,
    *,
    status: str = "✅",
    synced_at: str | None = SYNCED_1,
    repeated_suffix: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = ""
    if synced_at is not None:
        marker = f"<!-- Last synced: {synced_at} by sync-project-docs v3.2 -->"
        if repeated_suffix:
            marker += " by sync-project-docs v3.2 -->"
        marker += "\n"
    path.write_text(
        f"# {title}\n\n{marker}\n## 状态\n\n{status} 完整\n",
        encoding="utf-8",
    )


def _make_docs(tmp_path: Path) -> Path:
    root = tmp_path / "project_docs"
    root.mkdir()
    _write_doc(root / "root.md", "根文档")
    _write_doc(root / "group" / "child.md", "子文档", status="🔶")
    return root


def test_valid_manifest_paths_and_depth() -> None:
    assert contract.validate_manifest_relative_path("root.md").as_posix() == "root.md"
    assert contract.validate_manifest_relative_path("a/b.md").as_posix() == "a/b.md"
    assert contract.manifest_path_depth("root.md") == 0
    assert contract.manifest_path_depth("a/b.md") == 1
    assert contract.manifest_path_depth("a/b/c.md") == 2


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute.md",
        "C:/absolute.md",
        "C:relative.md",
        "//server/share.md",
        "bad\x00name.md",
        r"a\b.md",
        ".",
        "..",
        "./a.md",
        "a/../b.md",
        "a/./b.md",
        "a//b.md",
        "a/",
        "not-markdown.txt",
        ".md",
        "upper.MD",
        "folder/report:meta.md",
        "folder/bad?.md",
        "folder/name./child.md",
        "folder/name /child.md",
        "CON.md",
        "folder/LPT1.log.md",
        "bad\x01name.md",
    ],
)
def test_invalid_manifest_paths(value: str) -> None:
    with pytest.raises(contract.PathValidationError):
        contract.validate_manifest_relative_path(value)


def test_casefold_path_conflicts_are_rejected() -> None:
    with pytest.raises(contract.PathConflictError, match="case conflict"):
        contract.ensure_unique_manifest_paths(["Folder/A.md", "folder/a.md"])
    with pytest.raises(contract.PathConflictError, match="duplicate"):
        contract.ensure_unique_manifest_paths(["same.md", "same.md"])


def test_manifest_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    outside = tmp_path / "outside.md"
    _write_doc(outside, "外部")
    link = root / "escape.md"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(contract.PathValidationError, match="outside"):
        contract.resolve_manifest_document(root, "escape.md")


def test_manifest_discovery_rejects_external_directory_link_and_loop(
    tmp_path: Path,
) -> None:
    root = _make_docs(tmp_path)
    outside = tmp_path / "outside_docs"
    outside.mkdir()
    _write_doc(outside / "secret.md", "外部")
    external_link = root / "external"
    try:
        external_link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(contract.PathValidationError, match="symlink or reparse point"):
        contract.build_manifest(root, last_synced=SYNCED_1)

    external_link.unlink()
    loop = root / "loop"
    loop.symlink_to(root, target_is_directory=True)
    with pytest.raises(contract.PathValidationError, match="symlink or reparse point"):
        contract.build_manifest(root, last_synced=SYNCED_1)


def test_markdown_link_rules_and_structured_results(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    source = root / "a" / "source.md"
    target = root / "b" / "target.md"
    encoded_target = root / "b" / "目标 文档.md"
    _write_doc(source, "源")
    _write_doc(target, "目标")
    _write_doc(encoded_target, "编码目标")

    content = "\n".join(
        [
            "[extensionless](../b/target)",
            "[encoded](<../b/%E7%9B%AE%E6%A0%87%20%E6%96%87%E6%A1%A3.md?mode=1#part>)",
            "[web](https://example.com/x.md)",
            "[mail](mailto:test@example.com)",
            "[anchor](#section)",
            "[source](../code/tool.py)",
            "[other](../manual.pdf)",
            "[outside](../../../outside.md)",
            "[encoded-outside](%2E%2E/%2E%2E/outside.md)",
            "[encoded-backslash](..%5Cb%5Ctarget.md)",
            "[encoded-nul](../b/target%00.md)",
            "[ads](../b/report:meta.md)",
            "[device](../b/CON.md)",
            "[absolute](/absolute.md)",
            "[bad-percent](../b/%FF.md)",
        ]
    )
    links = contract.parse_markdown_links(
        content,
        source_doc=source,
        docs_root=root,
    )
    by_text = {link.text: link for link in links}

    extensionless = by_text["extensionless"]
    assert extensionless.disposition is contract.LinkDisposition.LOCAL_DOCUMENT
    assert extensionless.project_relative_path == "b/target.md"
    assert extensionless.exists is True

    encoded = by_text["encoded"]
    assert encoded.disposition is contract.LinkDisposition.LOCAL_DOCUMENT
    assert encoded.project_relative_path == "b/目标 文档.md"
    assert encoded.query == "mode=1"
    assert encoded.fragment == "part"
    assert encoded.exists is True

    for text in ("web", "mail", "anchor", "source", "other"):
        assert by_text[text].disposition is contract.LinkDisposition.SKIPPED
    for text in (
        "outside",
        "encoded-outside",
        "encoded-backslash",
        "encoded-nul",
        "ads",
        "device",
        "absolute",
        "bad-percent",
    ):
        assert by_text[text].disposition is contract.LinkDisposition.INVALID


def test_reference_style_and_html_links_are_resolved(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    source = root / "refs" / "source.md"
    target = root / "target.md"
    _write_doc(source, "源")
    _write_doc(target, "目标")
    content = "\n".join(
        [
            "[full][target-id]",
            "[collapsed][]",
            "[shortcut]",
            '<a href="../target.md#html">HTML</a>',
            "",
            "[target-id]: ../target.md",
            "[collapsed]: <../target.md?mode=1>",
            "[shortcut]: ../target",
        ]
    )

    links = contract.parse_markdown_links(
        content,
        source_doc=source,
        docs_root=root,
    )
    by_text = {link.text: link for link in links if link.text}

    assert by_text["full"].project_relative_path == "target.md"
    assert by_text["collapsed"].query == "mode=1"
    assert by_text["shortcut"].project_relative_path == "target.md"
    html = next(link for link in links if link.text == "")
    assert html.project_relative_path == "target.md"
    assert html.fragment == "html"


def test_link_resolution_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    source = root / "source.md"
    _write_doc(source, "源")
    outside = tmp_path / "outside.md"
    _write_doc(outside, "外部")
    link = root / "linked.md"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    result = contract.resolve_markdown_target(
        "linked.md",
        source_doc=source,
        docs_root=root,
    )
    assert result.disposition is contract.LinkDisposition.INVALID
    assert result.reason == "target-outside-docs-root"


def test_read_markdown_links_rejects_external_source_before_read(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"secret-\xff")
    source = root / "source.md"
    try:
        source.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(contract.LinkResolutionError, match="outside"):
        contract.read_markdown_links(source, root)


def test_secure_document_read_rejects_swap_to_external_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    victim = root / "victim.md"
    outside = tmp_path / "outside.md"
    _write_doc(victim, "原始")
    _write_doc(outside, "外部")
    original_resolve = contract.resolve_manifest_document
    swapped = False

    def swapping_resolve(*args: object, **kwargs: object) -> contract.SafeManifestPath:
        nonlocal swapped
        safe_path = original_resolve(*args, **kwargs)
        if safe_path.relative_path == "victim.md" and not swapped:
            try:
                victim.unlink()
                victim.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
            swapped = True
        return safe_path

    monkeypatch.setattr(contract, "resolve_manifest_document", swapping_resolve)
    with pytest.raises(contract.PathValidationError):
        contract.read_project_document(root, "victim.md")


def test_compute_relative_doc_link_uses_posix_paths(tmp_path: Path) -> None:
    root = _make_docs(tmp_path)
    link = contract.compute_relative_doc_link(
        "group/child.md",
        "root.md",
        root,
    )
    assert link == "../root.md"

    future_link = contract.compute_relative_doc_link(
        "new/source.md",
        "future/target.md",
        root,
    )
    assert future_link == "../future/target.md"
    with pytest.raises(contract.PathValidationError):
        contract.compute_relative_doc_link(
            "new/source.md",
            "future/target.md",
            root,
            must_exist=True,
        )


def test_last_synced_parser_ignores_repeated_tail(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    doc = root / "marker.md"
    _write_doc(doc, "Marker", repeated_suffix=True)
    content = contract.read_utf8(doc)
    assert contract.parse_last_synced_marker(content) == SYNCED_1
    metadata = contract.parse_document_metadata(content, "marker.md")
    assert metadata.title == "Marker"
    assert metadata.status == "✅"
    assert metadata.synced_at == SYNCED_1


def test_strict_utf8_and_json_reject_corruption(tmp_path: Path) -> None:
    bad_text = tmp_path / "bad.md"
    bad_text.write_bytes(b"valid\xffinvalid")
    with pytest.raises(contract.StrictUtf8Error):
        contract.read_utf8(bad_text)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"a": 1, "a": 2}\n', encoding="utf-8")
    with pytest.raises(contract.JsonContractError, match="duplicate"):
        contract.read_json_strict(duplicate)

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value": NaN}\n', encoding="utf-8")
    with pytest.raises(contract.JsonContractError, match="non-standard"):
        contract.read_json_strict(nonfinite)

    overflow = tmp_path / "overflow.json"
    overflow.write_text('{"positive": 1e309, "negative": -1e309}\n', encoding="utf-8")
    with pytest.raises(contract.JsonContractError, match="non-finite"):
        contract.read_json_strict(overflow)

    surrogate = tmp_path / "surrogate.json"
    surrogate.write_text('{"value": "\\ud800"}\n', encoding="utf-8")
    with pytest.raises(contract.JsonContractError, match="surrogate"):
        contract.read_json_strict(surrogate)


def test_atomic_json_is_stable_utf8_and_uses_unique_same_dir_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "manifest.json"
    destination.write_text("old", encoding="utf-8")
    real_replace = contract.os.replace
    temporary_paths: list[Path] = []

    def recording_replace(source: str | Path, target: str | Path) -> None:
        temporary_paths.append(Path(source))
        real_replace(source, target)

    monkeypatch.setattr(contract.os, "replace", recording_replace)
    contract.atomic_write_json(destination, {"z": 1, "a": "中文"})
    first_content = destination.read_text(encoding="utf-8")
    contract.atomic_write_json(destination, {"a": "中文", "z": 2})

    assert first_content == '{\n  "a": "中文",\n  "z": 1\n}\n'
    assert contract.read_json_strict(destination) == {"a": "中文", "z": 2}
    assert len(temporary_paths) == 2
    assert temporary_paths[0] != temporary_paths[1]
    assert all(path.parent == destination.parent for path in temporary_paths)
    assert not list(tmp_path.glob(".manifest.json.*.tmp"))


def test_exclusive_lock_blocks_a_second_owner(tmp_path: Path) -> None:
    lock_path = tmp_path / "manifest.lock"
    with contract.exclusive_lock(lock_path, timeout=1.0):
        with pytest.raises(contract.LockTimeoutError):
            with contract.exclusive_lock(
                lock_path,
                timeout=0.05,
                poll_interval=0.01,
            ):
                pass
    with contract.exclusive_lock(lock_path, timeout=0.2):
        pass


def test_exclusive_lock_rejects_symlink_without_modifying_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_bytes(b"")
    lock_path = tmp_path / "manifest.lock"
    try:
        lock_path.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(contract.LockError, match="link or reparse point"):
        with contract.exclusive_lock(lock_path, timeout=0.01):
            pass

    assert target.read_bytes() == b""


def test_exclusive_lock_repairs_uninitialized_existing_empty_file(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "manifest.lock"
    lock_path.write_bytes(b"")

    with contract.exclusive_lock(lock_path, timeout=0.1):
        pass
    assert lock_path.read_bytes() == b"\0"


def test_exclusive_lock_recovers_after_initialization_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "manifest.lock"
    original_write = contract.os.write

    def fail_write(fd: int, data: bytes) -> int:
        raise OSError("injected initialization failure")

    monkeypatch.setattr(contract.os, "write", fail_write)
    with pytest.raises(contract.LockError, match="cannot safely open"):
        with contract.exclusive_lock(lock_path, timeout=0.01):
            pass
    assert lock_path.read_bytes() == b""

    monkeypatch.setattr(contract.os, "write", original_write)
    with contract.exclusive_lock(lock_path, timeout=0.1):
        pass
    assert lock_path.read_bytes() == b"\0"


def test_build_and_write_manifest_locks_build_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    destination = root / contract.MANIFEST_FILENAME
    expected_project_root = root.parent.resolve()
    expected_manifest_lock = destination.with_name(destination.name + ".lock")
    events: list[tuple[str, Path]] = []
    expected_manifest = {"schema_version": contract.MANIFEST_SCHEMA_VERSION}

    @contextmanager
    def recording_project_lock(
        project_root: Path,
        **kwargs: object,
    ) -> object:
        assert kwargs == {"timeout": 2.0}
        events.append(("project-lock-enter", Path(project_root)))
        try:
            yield object()
        finally:
            events.append(("project-lock-exit", Path(project_root)))

    @contextmanager
    def recording_manifest_lock(
        lock_path: Path,
        **kwargs: object,
    ) -> object:
        assert kwargs == {"timeout": 2.0}
        events.append(("manifest-lock-enter", Path(lock_path)))
        try:
            yield object()
        finally:
            events.append(("manifest-lock-exit", Path(lock_path)))

    def recording_build(docs_root: Path, **kwargs: object) -> dict[str, int]:
        assert events == [
            ("project-lock-enter", expected_project_root),
            ("manifest-lock-enter", expected_manifest_lock),
        ]
        assert Path(docs_root) == root
        assert kwargs["last_synced"] == SYNCED_1
        events.append(("build", root))
        return expected_manifest

    def recording_write(path: Path, value: object) -> Path:
        assert events == [
            ("project-lock-enter", expected_project_root),
            ("manifest-lock-enter", expected_manifest_lock),
            ("build", root),
        ]
        assert value is expected_manifest
        events.append(("write", Path(path)))
        return Path(path)

    monkeypatch.setattr(contract, "project_sync_lock", recording_project_lock)
    monkeypatch.setattr(contract, "exclusive_lock", recording_manifest_lock)
    monkeypatch.setattr(contract, "build_manifest", recording_build)
    monkeypatch.setattr(contract, "atomic_write_json", recording_write)

    returned = contract.build_and_write_manifest(
        root,
        last_synced=SYNCED_1,
        timeout=2.0,
    )

    assert returned is expected_manifest
    assert events == [
        ("project-lock-enter", expected_project_root),
        ("manifest-lock-enter", expected_manifest_lock),
        ("build", root),
        ("write", destination),
        ("manifest-lock-exit", expected_manifest_lock),
        ("project-lock-exit", expected_project_root),
    ]


def test_manifest_v2_roundtrip_and_stable_generation(tmp_path: Path) -> None:
    root = _make_docs(tmp_path)
    first = contract.build_manifest(root, last_synced=SYNCED_1)
    second = contract.build_manifest(root, last_synced=SYNCED_2)

    assert first["schema_version"] == 2
    assert first["producer"] == {"name": "sync-project-docs", "version": "3.2"}
    assert first["docs_root"] == "project_docs"
    assert first["total"] == 2
    assert first["depth_counts"] == {"0": 1, "1": 1}
    assert [entry["path"] for entry in first["files"]] == [
        "group/child.md",
        "root.md",
    ]
    assert [entry["depth"] for entry in first["files"]] == [1, 0]
    assert first["generation_id"] == second["generation_id"]
    assert (
        contract.compute_generation_id(list(reversed(first["files"])))
        == first["generation_id"]
    )

    manifest_path = root / contract.MANIFEST_FILENAME
    contract.write_manifest(manifest_path, first, use_lock=False)
    loaded = contract.load_manifest(manifest_path, docs_root=root)
    assert loaded == first
    assert manifest_path.read_bytes().endswith(b"\n")


def test_manifest_v2_detects_document_and_manifest_hash_tampering(
    tmp_path: Path,
) -> None:
    root = _make_docs(tmp_path)
    manifest = contract.build_manifest(root, last_synced=SYNCED_1)
    manifest_path = root / contract.MANIFEST_FILENAME
    contract.atomic_write_json(manifest_path, manifest)

    (root / "root.md").write_text("# changed\n", encoding="utf-8")
    with pytest.raises(contract.ManifestFileMismatchError):
        contract.load_manifest(manifest_path, docs_root=root)
    assert (
        contract.load_manifest(
            manifest_path,
            docs_root=root,
            verify_files=False,
        )["generation_id"]
        == manifest["generation_id"]
    )

    tampered = copy.deepcopy(manifest)
    tampered["files"][0]["sha256"] = "0" * 64
    with pytest.raises(
        contract.ManifestValidationError, match="generation_id mismatch"
    ):
        contract.validate_manifest_v2(tampered, root, verify_files=False)


def test_verified_manifest_requires_complete_active_document_set(
    tmp_path: Path,
) -> None:
    root = _make_docs(tmp_path)
    manifest = contract.build_manifest(root, last_synced=SYNCED_1)
    extra = root / "extra.md"
    _write_doc(extra, "Extra", status="✅", synced_at=SYNCED_1)

    with pytest.raises(
        contract.ManifestFileMismatchError,
        match="unregistered active documents",
    ):
        contract.validate_manifest_v2(manifest, root, verify_files=True)

    assert (
        contract.validate_manifest_v2(
            manifest,
            root,
            verify_files=True,
            verify_complete_set=False,
        )["total"]
        == 2
    )
    assert (
        contract.validate_manifest_v2(
            manifest,
            root,
            verify_files=False,
        )["total"]
        == 2
    )

    manifest_path = root / contract.MANIFEST_FILENAME
    contract.write_manifest(manifest_path, manifest, use_lock=False)
    assert (
        contract.load_manifest(
            manifest_path,
            docs_root=root,
            verify_complete_set=False,
        )["total"]
        == 2
    )


def test_manifest_v2_verifies_document_metadata(tmp_path: Path) -> None:
    root = _make_docs(tmp_path)
    manifest = contract.build_manifest(root, last_synced=SYNCED_1)

    wrong_synced_at = copy.deepcopy(manifest)
    wrong_synced_at["files"][0]["synced_at"] = SYNCED_2
    with pytest.raises(contract.ManifestFileMismatchError, match="synced_at mismatch"):
        contract.validate_manifest_v2(wrong_synced_at, root, verify_files=True)
    contract.validate_manifest_v2(wrong_synced_at, root, verify_files=False)

    wrong_title = copy.deepcopy(manifest)
    wrong_title["files"][0]["title"] = "伪造标题"
    wrong_title["generation_id"] = contract.compute_generation_id(wrong_title["files"])
    with pytest.raises(contract.ManifestFileMismatchError, match="title mismatch"):
        contract.validate_manifest_v2(wrong_title, root, verify_files=True)

    invalid_status = copy.deepcopy(manifest)
    invalid_status["files"][0]["status"] = "complete"
    invalid_status["generation_id"] = contract.compute_generation_id(
        invalid_status["files"]
    )
    with pytest.raises(contract.ManifestValidationError, match="status must be one of"):
        contract.validate_manifest_v2(invalid_status, root, verify_files=False)


def test_manifest_v2_verify_files_false_allows_missing_documents(
    tmp_path: Path,
) -> None:
    root = _make_docs(tmp_path)
    manifest = contract.build_manifest(root, last_synced=SYNCED_1)
    (root / "root.md").unlink()

    contract.validate_manifest_v2(manifest, root, verify_files=False)
    with pytest.raises(contract.PathValidationError, match="cannot be safely resolved"):
        contract.validate_manifest_v2(manifest, root, verify_files=True)


def test_manifest_v2_rejects_inconsistent_aggregates(tmp_path: Path) -> None:
    root = _make_docs(tmp_path)
    manifest = contract.build_manifest(root, last_synced=SYNCED_1)

    wrong_total = copy.deepcopy(manifest)
    wrong_total["total"] += 1
    with pytest.raises(contract.ManifestValidationError, match="total mismatch"):
        contract.validate_manifest_v2(wrong_total, root)

    wrong_depths = copy.deepcopy(manifest)
    wrong_depths["depth_counts"] = {"0": 2}
    with pytest.raises(contract.ManifestValidationError, match="depth_counts mismatch"):
        contract.validate_manifest_v2(wrong_depths, root)


def test_v1_depth_zero_is_recomputed_in_memory_without_writeback(
    tmp_path: Path,
) -> None:
    root = _make_docs(tmp_path)
    manifest_path = root / contract.MANIFEST_FILENAME
    v1 = {
        "files": [
            {"path": "root.md", "depth": 0, "title": "根文档", "status": "✅"},
            {
                "path": "group/child.md",
                "depth": 0,
                "title": "子文档",
                "status": "🔶",
            },
        ],
        "total": 2,
        "last_synced": SYNCED_1,
    }
    contract.atomic_write_json(manifest_path, v1)

    with pytest.warns(
        contract.LegacyManifestWarning, match="depth values are untrusted"
    ):
        loaded = contract.load_manifest(manifest_path, docs_root=root)

    by_path = {entry["path"]: entry for entry in loaded["files"]}
    assert loaded["schema_version"] == 1
    assert by_path["root.md"]["depth"] == 0
    assert by_path["group/child.md"]["depth"] == 1
    assert by_path["group/child.md"]["bytes"] > 0
    assert len(by_path["group/child.md"]["sha256"]) == 64
    assert by_path["group/child.md"]["synced_at"] == SYNCED_1

    on_disk = contract.read_json_strict(manifest_path)
    assert "schema_version" not in on_disk
    assert on_disk["files"][1]["depth"] == 0

    with pytest.warns(contract.LegacyManifestWarning):
        loaded_again = contract.validate_manifest(loaded, root)
    assert loaded_again["files"] == loaded["files"]


@pytest.mark.parametrize("schema_version", [2.0, True, "2", None])
def test_manifest_schema_version_requires_an_exact_integer(
    tmp_path: Path,
    schema_version: object,
) -> None:
    root = _make_docs(tmp_path)
    manifest = contract.build_manifest(root, last_synced=SYNCED_1)
    manifest["schema_version"] = schema_version

    with pytest.raises(contract.ManifestSchemaError):
        contract.validate_manifest(manifest, root)


def test_unknown_manifest_schema_fails(tmp_path: Path) -> None:
    root = _make_docs(tmp_path)
    with pytest.raises(contract.ManifestSchemaError, match="unsupported"):
        contract.validate_manifest({"schema_version": 99}, root)


def test_build_manifest_rejects_non_utf8_document(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    root.mkdir()
    (root / "bad.md").write_bytes(b"# title\n\xff")
    with pytest.raises(contract.StrictUtf8Error):
        contract.build_manifest(root, last_synced=SYNCED_1)


def test_build_manifest_excludes_only_root_quarantine_documents(tmp_path: Path) -> None:
    root = _make_docs(tmp_path)
    _write_doc(root / contract.QUARANTINE_DIRNAME / "receipt" / "old.md", "旧文档")
    _write_doc(root / ".lingma" / "notes.md", "隐藏但活动的文档")
    _write_doc(
        root / "section" / contract.QUARANTINE_DIRNAME / "notes.md",
        "嵌套同名目录仍属活动树",
    )

    manifest = contract.build_manifest(root, last_synced=SYNCED_1)
    paths = {entry["path"] for entry in manifest["files"]}

    assert manifest["total"] == 4
    assert ".lingma/notes.md" in paths
    assert "section/.quarantine/notes.md" in paths
    assert all(
        not entry["path"].startswith(contract.QUARANTINE_DIRNAME + "/")
        for entry in manifest["files"]
    )


def test_build_manifest_rejects_noncanonical_root_quarantine_name(
    tmp_path: Path,
) -> None:
    root = _make_docs(tmp_path)
    (root / ".QUARANTINE").mkdir()

    with pytest.raises(contract.PathValidationError, match="canonical lowercase"):
        contract.build_manifest(root, last_synced=SYNCED_1)
