# -*- coding: utf-8 -*-
"""Focused safety and transaction tests for ``phase4_exit``."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

import pytest

import phase4_exit
import project_docs_contract as contract

SYNCED_AT = "2026-07-30T12:00:00+08:00"


def _assert_only_inactive_cleanup_residue(quarantine_root: Path) -> None:
    entries = list(quarantine_root.iterdir())
    unexpected = [
        path
        for path in entries
        if path.name != ".phase4.lock"
        and not (
            os.name == "posix" and path.name.startswith(phase4_exit._ARCHIVE_PREFIX)
        )
    ]
    assert unexpected == []


def _make_root(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    docs_root = root / "project_docs"
    docs_root.mkdir(parents=True)
    return root, docs_root


def _write_doc(
    path: Path,
    title: str = "文档",
    *,
    exact_marker: bool = True,
    overview: str | None = "目录说明。",
    body: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = (
        phase4_exit.AUTO_MARKER
        if exact_marker
        else "<!-- AUTO-GENERATED v3.0: folder-doc -->"
    )
    sections = [
        f"# {title}",
        "",
        marker,
        f"<!-- Last synced: {SYNCED_AT} by sync-project-docs v3.2 -->",
    ]
    if overview is not None:
        sections.extend(["", "## 概述", "", overview])
    if body:
        sections.extend(["", body])
    sections.extend(["", "## 状态", "", "✅ 完整", ""])
    path.write_text("\n".join(sections), encoding="utf-8")


def _write_manifest(docs_root: Path) -> dict:
    manifest = contract.build_manifest(docs_root, last_synced=SYNCED_AT)
    contract.atomic_write_json(docs_root / contract.MANIFEST_FILENAME, manifest)
    return manifest


def _write_supported_claude(root: Path) -> Path:
    indexer = phase4_exit.claude_doc_index
    lines = ["# CLAUDE.md", "manual-before"]
    for section in indexer.REQUIRED_SECTIONS:
        lines.extend(
            [
                indexer.section_start(section),
                f"old-{section}",
                indexer.section_end(section),
                f"manual-after-{section}",
            ]
        )
    path = root / "CLAUDE.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _run_json(
    capsys: pytest.CaptureFixture[str],
    root: Path,
    *arguments: str,
) -> tuple[int, dict]:
    exit_code = phase4_exit.main([str(root), *arguments, "--json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert captured.out == contract.stable_json_dumps(payload)
    assert payload["exit_code"] == exit_code
    return exit_code, payload


def _eligible_project(
    tmp_path: Path, name: str = "orphan.md"
) -> tuple[Path, Path, Path]:
    root, docs_root = _make_root(tmp_path)
    document = docs_root / name
    _write_doc(document, title="待退出目录")
    _write_manifest(docs_root)
    return root, docs_root, document


def _apply_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, Path, Path, dict, Path]:
    root, docs_root, document = _eligible_project(tmp_path)
    exit_code, payload = _run_json(capsys, root, "--apply")
    assert exit_code == phase4_exit.EXIT_OK, contract.stable_json_dumps(payload)
    assert payload["ok"] is True
    assert len(payload["quarantined"]) == 1
    receipt_id = payload["quarantined"][0]["receipt_id"]
    receipt_dir = docs_root / phase4_exit.QUARANTINE_DIRNAME / receipt_id
    return root, docs_root, document, payload, receipt_dir


def test_default_report_is_non_mutating_and_json_is_pure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)

    exit_code, payload = _run_json(capsys, root)

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["action"] == "report"
    assert payload["mode"] == "report-only"
    assert payload["counts"]["candidates"] == 1
    assert payload["candidates"][0]["path"] == "orphan.md"
    assert document.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("--dry-run",),
        ("--auto-approve", "A"),
        ("--auto-approve", "A+B"),
        ("--dry-run", "--auto-approve", "A+B"),
    ],
)
def test_legacy_report_options_never_mutate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    original = document.read_bytes()

    exit_code, payload = _run_json(capsys, root, *arguments)

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["action"] == "report"
    assert payload["mode"] == "report-only"
    assert payload["warnings"]
    assert document.read_bytes() == original
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


@pytest.mark.parametrize(
    ("manifest_text", "expected_code"),
    [
        (None, "manifest-missing"),
        ('{"files": [], "files": []}\n', "manifest-invalid"),
        ('{"schema_version": 99}\n', "manifest-invalid"),
    ],
)
def test_missing_or_malformed_manifest_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    manifest_text: str | None,
    expected_code: str,
) -> None:
    root, docs_root = _make_root(tmp_path)
    document = docs_root / "orphan.md"
    _write_doc(document)
    if manifest_text is not None:
        (docs_root / contract.MANIFEST_FILENAME).write_text(
            manifest_text,
            encoding="utf-8",
        )

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == expected_code
    assert document.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_manifest_hash_mismatch_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    document.write_text(
        document.read_text(encoding="utf-8") + "changed\n", encoding="utf-8"
    )

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "manifest-invalid"
    assert document.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_unregistered_document_blocks_the_entire_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, candidate = _eligible_project(tmp_path)
    rogue = docs_root / "rogue.md"
    _write_doc(rogue, title="未注册")

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["unregistered"] == ["rogue.md"]
    assert any(error["code"] == "unregistered-documents" for error in payload["errors"])
    assert candidate.is_file()
    assert rogue.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_exact_marker_is_required_and_empty_content_is_not_a_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root = _make_root(tmp_path)
    markerless = docs_root / "markerless.md"
    _write_doc(markerless, exact_marker=False, overview=None)
    _write_manifest(docs_root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["quarantined"] == []
    assert payload["blocked"] == [
        {
            "inbound_references": 0,
            "inbound_sources": [],
            "path": "markerless.md",
            "reason": "missing-exact-auto-marker",
            "source_path": "markerless",
        }
    ]
    assert markerless.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_short_or_missing_overview_does_not_override_real_safety_gates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root = _make_root(tmp_path)
    document = docs_root / "short.md"
    _write_doc(document, overview=None)
    _write_manifest(docs_root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert len(payload["quarantined"]) == 1
    assert not document.exists()


@pytest.mark.parametrize("boundary_filename", ["CLAUDE.md", "claude.md", "Claude.md"])
def test_boundary_descendant_is_never_a_candidate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    boundary_filename: str,
) -> None:
    root, docs_root = _make_root(tmp_path)
    source_child = root / "boundary" / "child"
    source_child.mkdir(parents=True)
    (root / "boundary" / boundary_filename).write_text(
        "# Boundary\n",
        encoding="utf-8",
    )
    document = docs_root / "boundary" / "child.md"
    _write_doc(document)
    _write_manifest(docs_root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["candidates"] == []
    assert payload["blocked"][0]["reason"] == "boundary-descendant"
    assert payload["blocked"][0]["boundary"] == "boundary"
    assert document.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


@pytest.mark.parametrize(
    ("target_name", "link_target"),
    [
        ("target.md", "../target"),
        ("gone target.md", "../gone%20target?mode=1#part"),
    ],
)
def test_shared_markdown_resolver_counts_structural_inbound_links(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    target_name: str,
    link_target: str,
) -> None:
    root, docs_root = _make_root(tmp_path)
    target = docs_root / target_name
    referrer = docs_root / "refs" / "source.md"
    _write_doc(target, title="目标")
    _write_doc(referrer, title="引用者", body=f"[target]({link_target})")
    (root / "refs" / "source").mkdir(parents=True)
    _write_manifest(docs_root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    blocked = {item["path"]: item for item in payload["blocked"]}
    assert blocked[target_name]["reason"] == "inbound-references"
    assert blocked[target_name]["inbound_references"] == 1
    assert blocked[target_name]["inbound_sources"] == ["refs/source.md"]
    assert target.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_reference_style_inbound_link_blocks_quarantine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root = _make_root(tmp_path)
    target = docs_root / "target.md"
    referrer = docs_root / "refs" / "source.md"
    _write_doc(target, title="目标")
    _write_doc(
        referrer,
        title="引用者",
        body="[目标][target-id]\n\n[target-id]: ../target.md",
    )
    (root / "refs" / "source").mkdir(parents=True)
    _write_manifest(docs_root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    blocked = {item["path"]: item for item in payload["blocked"]}
    assert blocked["target.md"]["reason"] == "inbound-references"
    assert blocked["target.md"]["inbound_sources"] == ["refs/source.md"]
    assert target.is_file()


def test_invalid_local_markdown_target_blocks_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root = _make_root(tmp_path)
    candidate = docs_root / "candidate.md"
    referrer = docs_root / "refs" / "source.md"
    _write_doc(candidate)
    _write_doc(referrer, body="[unsafe](../../../outside.md)")
    (root / "refs" / "source").mkdir(parents=True)
    _write_manifest(docs_root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert any(
        error["code"] == "invalid-markdown-target" for error in payload["errors"]
    )
    assert candidate.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_quarantine_is_excluded_from_candidates_and_reference_scan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, candidate = _eligible_project(tmp_path, "candidate.md")
    old_payload = (
        docs_root
        / phase4_exit.QUARANTINE_DIRNAME
        / "old-receipt"
        / "document"
        / "ignored.md"
    )
    old_payload.parent.mkdir(parents=True)
    old_payload.write_bytes(b"[candidate](../../../candidate)\n\xff")

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert len(payload["quarantined"]) == 1
    assert not candidate.exists()
    assert old_payload.read_bytes().endswith(b"\xff")
    assert payload["counts"]["active_documents"] == 1


def test_apply_moves_to_receipt_with_required_integrity_metadata(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    original = document.read_bytes()
    expected_hash = contract.sha256_file(document)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert not document.exists()
    receipt_id = payload["quarantined"][0]["receipt_id"]
    receipt_dir = docs_root / phase4_exit.QUARANTINE_DIRNAME / receipt_id
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    quarantined = receipt_dir / receipt["quarantined_path"]
    assert quarantined.read_bytes() == original
    assert receipt["original_path"] == "orphan.md"
    assert receipt["sha256"] == expected_hash
    assert receipt["reason"] == phase4_exit.QUARANTINE_REASON
    assert receipt["generation"] == payload["generation"]
    assert receipt["state"] == "quarantined"
    assert receipt["restored_at"] is None
    assert quarantined.is_file()
    assert "os.remove" not in Path(phase4_exit.__file__).read_text(encoding="utf-8")


def test_receipt_write_failure_rolls_document_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    original = document.read_bytes()

    real_write_snapshot = phase4_exit._write_exclusive_snapshot

    def fail_receipt_write(
        path: Path,
        base: Path,
        description: str,
        snapshot: dict,
        *,
        staging_root: Path | None = None,
        staging_trusted_root: Path | None = None,
    ) -> phase4_exit.ExclusiveCopy:
        if description.startswith("quarantine receipt "):
            raise OSError("injected receipt failure")
        return real_write_snapshot(
            path,
            base,
            description,
            snapshot,
            staging_root=staging_root,
            staging_trusted_root=staging_trusted_root,
        )

    monkeypatch.setattr(phase4_exit, "_write_exclusive_snapshot", fail_receipt_write)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][0]["code"] == "quarantine-transaction-failed"
    assert document.read_bytes() == original
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    assert quarantine_root.is_dir()
    _assert_only_inactive_cleanup_residue(quarantine_root)


def test_restore_by_receipt_succeeds_and_keeps_audit_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]

    exit_code, restore_payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OK, restore_payload
    assert restore_payload["restored"]["original_path"] == "orphan.md"
    assert document.is_file()
    assert contract.sha256_file(document) == receipt["sha256"]
    receipt_after = contract.read_json_strict(
        receipt_dir / phase4_exit.RECEIPT_FILENAME
    )
    assert receipt_after["state"] == "restored"
    assert isinstance(receipt_after["restored_at"], str)
    assert restore_payload["restored"]["no_op"] is False
    restored_manifest = contract.load_manifest(document.parent)
    assert [entry["path"] for entry in restored_manifest["files"]] == ["orphan.md"]
    assert not payload_path.exists()


def test_restore_refuses_existing_destination_without_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    document.write_text("conflict\n", encoding="utf-8")

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "restore-conflict"
    assert document.read_text(encoding="utf-8") == "conflict\n"
    assert payload_path.is_file()


def test_restore_refuses_payload_hash_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    payload_path.write_text("tampered\n", encoding="utf-8")

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "receipt-hash-mismatch"
    assert not document.exists()
    assert payload_path.is_file()


def test_restore_refuses_non_utf8_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    payload_path.write_bytes(b"\xff")
    receipt["sha256"] = contract.sha256_file(payload_path)
    contract.atomic_write_json(receipt_dir / phase4_exit.RECEIPT_FILENAME, receipt)

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "receipt-payload-invalid"
    assert not document.exists()


def test_restore_refuses_non_utf8_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    (receipt_dir / phase4_exit.RECEIPT_FILENAME).write_bytes(b"{\xff}")

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "receipt-invalid"
    assert not document.exists()


def test_restore_rejects_unsafe_receipt_id_before_path_use(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root = _make_root(tmp_path)
    (docs_root / phase4_exit.QUARANTINE_DIRNAME).mkdir()

    exit_code, payload = _run_json(capsys, root, "--restore", "../escape")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "invalid-receipt-id"


def test_restore_rejects_unsafe_original_path_in_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt = contract.read_json_strict(receipt_path)
    receipt["original_path"] = "../escape.md"
    contract.atomic_write_json(receipt_path, receipt)

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "receipt-invalid"
    assert not document.exists()


def test_active_document_symlink_escape_is_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, candidate = _eligible_project(tmp_path)
    outside = tmp_path / "outside.md"
    _write_doc(outside)
    link = docs_root / "linked.md"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "untrusted-path"
    assert candidate.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_non_utf8_active_unregistered_document_fails_closed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, candidate = _eligible_project(tmp_path)
    (docs_root / "bad.md").write_bytes(b"# valid\n\xff")

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "active-document-invalid"
    assert candidate.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_legacy_auto_approve_cannot_be_combined_with_apply(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)

    exit_code, payload = _run_json(
        capsys,
        root,
        "--apply",
        "--auto-approve",
        "A+B",
    )

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "legacy-option-refused"
    assert document.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_candidate_snapshot_uses_staged_docs_and_real_source_inventory(
    tmp_path: Path,
) -> None:
    root, _ = _make_root(tmp_path)
    (root / "present").mkdir()
    stage_docs = root / ".sync-project-docs-stage-test" / "project_docs"
    _write_doc(stage_docs / "present.md", title="现有目录")
    _write_doc(stage_docs / "orphan.md", title="退出候选")
    manifest = contract.build_manifest(stage_docs, last_synced=SYNCED_AT)

    analysis = phase4_exit.analyze_snapshot(root, stage_docs, manifest)
    payload = phase4_exit.analysis_result(
        analysis,
        action="report",
        mode="candidate-snapshot",
    )

    assert payload["ok"] is True
    assert [item["path"] for item in payload["candidates"]] == ["orphan.md"]
    assert payload["counts"]["active_documents"] == 2
    assert payload["counts"]["manifest_documents"] == 2
    assert (stage_docs / "orphan.md").is_file()


def test_candidate_snapshot_rejects_manifest_not_matching_stage(
    tmp_path: Path,
) -> None:
    root, _ = _make_root(tmp_path)
    stage_docs = root / ".sync-project-docs-stage-test" / "project_docs"
    _write_doc(stage_docs / "orphan.md")
    manifest = contract.build_manifest(stage_docs, last_synced=SYNCED_AT)
    (stage_docs / "orphan.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit.analyze_snapshot(root, stage_docs, manifest)

    assert caught.value.code == "manifest-invalid"


def test_apply_updates_v2_manifest_and_supported_claude_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root = _make_root(tmp_path)
    (root / "present").mkdir()
    present = docs_root / "present.md"
    orphan = docs_root / "orphan.md"
    _write_doc(present, title="present — 活动文档")
    _write_doc(orphan, title="orphan — 退出文档")
    _write_manifest(docs_root)
    claude_path = _write_supported_claude(root)

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert not orphan.exists()
    assert present.is_file()
    manifest = contract.load_manifest(docs_root)
    assert manifest["schema_version"] == contract.MANIFEST_SCHEMA_VERSION
    assert manifest["generation_id"] == payload["manifest_generation"]
    assert manifest["total"] == 1
    assert [entry["path"] for entry in manifest["files"]] == ["present.md"]
    claude_content = claude_path.read_text(encoding="utf-8")
    assert "old-doc-tree-instruction" not in claude_content
    assert "old-project-overview" in claude_content
    assert "present — 活动文档 ✅" in claude_content
    assert "orphan — 退出文档" not in claude_content


def test_all_candidates_are_prevalidated_before_first_move(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _make_root(tmp_path)
    first = docs_root / "a.md"
    second = docs_root / "b.md"
    _write_doc(first, title="A")
    _write_doc(second, title="B")
    _write_manifest(docs_root)
    real_revalidate = phase4_exit._revalidate_candidate
    visited: list[str] = []

    def fail_second(
        analysis: phase4_exit.Analysis,
        candidate: dict,
        expected_active: set[str],
    ) -> Path:
        visited.append(candidate["path"])
        if len(visited) == 2:
            raise phase4_exit.Phase4Refusal(
                "injected-prevalidation-failure",
                "second candidate failed before the batch started",
            )
        return real_revalidate(analysis, candidate, expected_active)

    monkeypatch.setattr(phase4_exit, "_revalidate_candidate", fail_second)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "injected-prevalidation-failure"
    assert visited == ["a.md", "b.md"]
    assert first.is_file() and second.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()
    assert contract.load_manifest(docs_root)["total"] == 2


def test_multi_candidate_receipt_failure_rolls_back_entire_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _make_root(tmp_path)
    documents = [docs_root / "a.md", docs_root / "b.md"]
    for document in documents:
        _write_doc(document, title=document.stem)
    manifest_before = docs_root / contract.MANIFEST_FILENAME
    _write_manifest(docs_root)
    manifest_bytes = manifest_before.read_bytes()
    real_write_snapshot = phase4_exit._write_exclusive_snapshot
    receipt_writes = 0

    def fail_second_receipt(
        path: Path,
        base: Path,
        description: str,
        snapshot: dict,
        *,
        staging_root: Path | None = None,
        staging_trusted_root: Path | None = None,
    ) -> phase4_exit.ExclusiveCopy:
        nonlocal receipt_writes
        if description.startswith("quarantine receipt "):
            receipt_writes += 1
            if receipt_writes == 2:
                raise OSError("injected second receipt failure")
        return real_write_snapshot(
            path,
            base,
            description,
            snapshot,
            staging_root=staging_root,
            staging_trusted_root=staging_trusted_root,
        )

    monkeypatch.setattr(
        phase4_exit,
        "_write_exclusive_snapshot",
        fail_second_receipt,
    )
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-transaction-failed"
    assert payload["quarantined"] == []
    assert all(document.is_file() for document in documents)
    assert manifest_before.read_bytes() == manifest_bytes
    assert contract.load_manifest(docs_root)["total"] == 2
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    assert quarantine_root.is_dir()
    _assert_only_inactive_cleanup_residue(quarantine_root)


def test_manifest_commit_failure_restores_documents_receipts_and_claude(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _make_root(tmp_path)
    (root / "present").mkdir()
    present = docs_root / "present.md"
    orphan = docs_root / "orphan.md"
    _write_doc(present, title="present")
    _write_doc(orphan, title="orphan")
    _write_manifest(docs_root)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    manifest_before = manifest_path.read_bytes()
    claude_path = _write_supported_claude(root)
    claude_before = claude_path.read_bytes()
    _fail_at(
        monkeypatch,
        "manifest-candidate-staged",
        OSError("injected manifest commit failure"),
    )

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-transaction-failed"
    assert orphan.is_file() and present.is_file()
    assert manifest_path.read_bytes() == manifest_before
    assert claude_path.read_bytes() == claude_before
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    assert quarantine_root.is_dir()
    _assert_only_inactive_cleanup_residue(quarantine_root)


def test_apply_reloads_snapshot_only_after_project_lock_is_entered(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    real_project_lock = phase4_exit.contract.project_sync_lock
    entered: list[Path] = []

    class MutatingLock:
        def __init__(self, inner: object) -> None:
            self.inner = inner

        def __enter__(self) -> object:
            lock = getattr(self.inner, "__enter__")()
            entered.append(root.resolve())
            (root / "orphan").mkdir()
            return lock

        def __exit__(self, *args: object) -> object:
            return getattr(self.inner, "__exit__")(*args)

    def wrapped_lock(project_root: Path, **kwargs: object) -> MutatingLock:
        assert Path(project_root).resolve() == root.resolve()
        return MutatingLock(real_project_lock(project_root, **kwargs))

    monkeypatch.setattr(phase4_exit.contract, "project_sync_lock", wrapped_lock)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert entered == [root.resolve()]
    assert payload["quarantined"] == []
    assert document.is_file()
    assert not (docs_root / phase4_exit.QUARANTINE_DIRNAME).exists()


def test_apply_and_restore_share_the_canonical_project_lock_domain(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, _ = _eligible_project(tmp_path)
    real_project_lock = phase4_exit.contract.project_sync_lock
    calls: list[Path] = []

    def tracking_lock(project_root: Path, **kwargs: object) -> object:
        calls.append(Path(project_root).resolve())
        return real_project_lock(project_root, **kwargs)

    monkeypatch.setattr(phase4_exit.contract, "project_sync_lock", tracking_lock)
    apply_code, apply_payload = _run_json(capsys, root, "--apply")
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    restore_code, restore_payload = _run_json(
        capsys,
        root,
        "--restore",
        receipt_id,
    )

    assert apply_code == phase4_exit.EXIT_OK, apply_payload
    assert restore_code == phase4_exit.EXIT_OK, restore_payload
    assert calls == [root.resolve(), root.resolve()]


def test_restore_concurrent_target_creation_is_atomic_no_replace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(
        tmp_path,
        capsys,
    )
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt_before = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt_before["quarantined_path"]
    real_exclusive_copy = phase4_exit._exclusive_copy_file

    def create_racing_target(
        source: Path,
        destination: Path,
        expected_digest: str,
        expected_identity: tuple[int, int, int, int],
        *,
        archive_root: Path,
        archive_trusted_root: Path,
        cleanup_on_failure: bool = True,
    ) -> phase4_exit.ExclusiveCopy:
        destination.write_text("concurrent creator\n", encoding="utf-8")
        return real_exclusive_copy(
            source,
            destination,
            expected_digest,
            expected_identity,
            archive_root=archive_root,
            archive_trusted_root=archive_trusted_root,
            cleanup_on_failure=cleanup_on_failure,
        )

    monkeypatch.setattr(
        phase4_exit,
        "_exclusive_copy_file",
        create_racing_target,
    )
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "restore-conflict"
    assert document.read_text(encoding="utf-8") == "concurrent creator\n"
    assert payload_path.is_file()
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    manifest = contract.read_json_strict(docs_root / contract.MANIFEST_FILENAME)
    assert manifest["total"] == 0


def test_restore_manifest_failure_rolls_back_target_and_receipt_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(
        tmp_path,
        capsys,
    )
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt_before = receipt_path.read_bytes()
    receipt = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt["quarantined_path"]
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    manifest_before = manifest_path.read_bytes()
    _fail_at(
        monkeypatch,
        "manifest-candidate-staged",
        OSError("injected restore manifest failure"),
    )

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "restore-transaction-failed"
    assert not document.exists()
    assert payload_path.is_file()
    assert receipt_path.read_bytes() == receipt_before
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    assert manifest_path.read_bytes() == manifest_before


def test_restore_is_idempotent_after_atomic_receipt_transition(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(
        tmp_path,
        capsys,
    )
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME

    first_code, first_payload = _run_json(capsys, root, "--restore", receipt_id)
    receipt_after_first = contract.read_json_strict(receipt_path)
    receipt_bytes = receipt_path.read_bytes()
    second_code, second_payload = _run_json(capsys, root, "--restore", receipt_id)

    assert first_code == phase4_exit.EXIT_OK
    assert first_payload["restored"]["no_op"] is False
    assert receipt_after_first["state"] == "restored"
    assert isinstance(receipt_after_first["restored_at"], str)
    assert second_code == phase4_exit.EXIT_OK
    assert second_payload["restored"]["no_op"] is True
    assert second_payload["counts"] == {"no_op": 1, "restored": 0}
    assert receipt_path.read_bytes() == receipt_bytes
    assert document.is_file()
    assert contract.sha256_file(document) == receipt_after_first["sha256"]
    manifest = contract.load_manifest(docs_root)
    assert [entry["path"] for entry in manifest["files"]] == ["orphan.md"]


class _SimulatedCrash(BaseException):
    """Test-only abrupt-stop stand-in; no process is actually terminated."""


def _crash_at(monkeypatch: pytest.MonkeyPatch, point_name: str) -> None:
    def crash(point: str) -> None:
        if point == point_name:
            raise _SimulatedCrash(point)

    monkeypatch.setattr(phase4_exit, "_transaction_fault", crash)


def _fail_at(
    monkeypatch: pytest.MonkeyPatch,
    point_name: str,
    error: Exception,
) -> None:
    def fail(point: str) -> None:
        if point == point_name:
            raise error

    monkeypatch.setattr(phase4_exit, "_transaction_fault", fail)


def _transaction_journals(docs_root: Path) -> list[Path]:
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    if not quarantine_root.exists():
        return []
    candidates = list(quarantine_root.glob(f"{phase4_exit._TRANSACTION_PREFIX}*.json"))
    canonical = quarantine_root / phase4_exit._TRANSACTION_CANONICAL_FILENAME
    if canonical.exists():
        candidates.append(canonical)
    return sorted(candidates)


def test_recover_quarantine_before_manifest_rolls_back_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    original = document.read_bytes()
    _crash_at(monkeypatch, "quarantine-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    assert not document.exists()
    assert len(_transaction_journals(docs_root)) == 2
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["status"] == "recovered"
    assert payload["recovery"]["outcome"] == "quarantine-rolled-back"
    assert document.read_bytes() == original
    assert contract.load_manifest(docs_root)["total"] == 1
    assert _transaction_journals(docs_root) == []

    repeat_code, repeat_payload = _run_json(capsys, root, "--recover")
    assert repeat_code == phase4_exit.EXIT_OK, repeat_payload
    assert repeat_payload["status"] == "no-pending-transaction"


def test_recover_quarantine_metadata_gap_restores_claude_preimage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    claude_path = _write_supported_claude(root)
    claude_before = claude_path.read_bytes()
    _crash_at(monkeypatch, "metadata-preimage-detached")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    assert not claude_path.exists()
    assert not document.exists()
    assert len(_transaction_journals(docs_root)) == 2
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "quarantine-rolled-back"
    assert claude_path.read_bytes() == claude_before
    assert document.is_file()
    assert contract.load_manifest(docs_root)["total"] == 1
    assert _transaction_journals(docs_root) == []


def test_recover_quarantine_after_manifest_finishes_cleanup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-manifest-committed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    assert not document.exists()
    journal = _transaction_journals(docs_root)
    assert len(journal) == 2
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "quarantine-finalized"
    assert contract.load_manifest(docs_root)["total"] == 0
    assert _transaction_journals(docs_root) == []


def test_recover_restore_copy_before_metadata_rolls_forward(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    _crash_at(monkeypatch, "restore-copy-complete")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    assert document.is_file()
    assert contract.sha256_file(document) == receipt["sha256"]
    assert not anchor_path.exists()
    assert phase4_exit._identity_from_stat(document.lstat()) != (
        phase4_exit._identity_from_stat(payload_path.lstat())
    )
    assert [path.name for path in _transaction_journals(docs_root)] == [
        phase4_exit._TRANSACTION_TARGET_FILENAME,
        phase4_exit._TRANSACTION_CANONICAL_FILENAME,
    ]
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-finalized"
    assert contract.load_manifest(docs_root)["total"] == 1
    receipt_after = contract.read_json_strict(
        receipt_dir / phase4_exit.RECEIPT_FILENAME
    )
    assert receipt_after["state"] == "restored"
    assert not (receipt_dir / receipt["quarantined_path"]).exists()
    assert _transaction_journals(docs_root) == []


def test_recover_prepared_restore_without_staged_target_cancels(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    _crash_at(monkeypatch, "restore-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    assert not document.exists()
    assert not anchor_path.exists()
    assert payload_path.is_file()
    assert [path.name for path in _transaction_journals(docs_root)] == [
        phase4_exit._TRANSACTION_CANONICAL_FILENAME
    ]
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-cancelled"
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    assert payload_path.is_file()
    assert not document.exists()
    assert contract.load_manifest(docs_root)["total"] == 0
    assert _transaction_journals(docs_root) == []


def test_recover_prepared_restore_with_unrecorded_staged_target_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    _crash_at(monkeypatch, "restore-staged-target-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    staged_identity = phase4_exit._identity_from_stat(anchor_path.lstat())
    assert staged_identity != phase4_exit._identity_from_stat(payload_path.lstat())
    journals_before = _transaction_journals(docs_root)
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-unowned-staged-target"
    assert not document.exists()
    assert phase4_exit._identity_from_stat(anchor_path.lstat()) == staged_identity
    assert payload_path.is_file()
    assert _transaction_journals(docs_root) == journals_before


def test_recover_target_created_restore_publishes_persisted_staged_inode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    _crash_at(monkeypatch, "restore-target-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    staged_identity = phase4_exit._identity_from_stat(anchor_path.lstat())
    target_journal_path = receipt_dir.parent / phase4_exit._TRANSACTION_TARGET_FILENAME
    target_journal = contract.read_json_strict(target_journal_path)
    assert tuple(target_journal["restore"]["target_identity"]) == staged_identity
    assert staged_identity != phase4_exit._identity_from_stat(payload_path.lstat())
    assert not document.exists()
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-finalized"
    assert not anchor_path.exists()
    assert phase4_exit._identity_from_stat(document.lstat()) == staged_identity
    assert not payload_path.exists()
    assert _transaction_journals(docs_root) == []


@pytest.mark.parametrize("failure_point", ["verification", "fsync"])
def test_recover_restore_rename_failure_retains_identity_for_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    target_journal_path = receipt_dir.parent / phase4_exit._TRANSACTION_TARGET_FILENAME
    _crash_at(monkeypatch, "restore-target-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    expected_identity = tuple(
        contract.read_json_strict(target_journal_path)["restore"]["target_identity"]
    )
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    if failure_point == "verification":
        real_capture = phase4_exit._capture_snapshot_with_identity

        def fail_destination_capture(
            path: Path,
            base: Path,
            description: str,
        ) -> tuple[dict[str, object], tuple[int, int, int, int] | None]:
            if description == "exclusive restore destination":
                raise OSError("injected destination verification failure")
            return real_capture(path, base, description)

        monkeypatch.setattr(
            phase4_exit,
            "_capture_snapshot_with_identity",
            fail_destination_capture,
        )
    else:
        real_fsync = phase4_exit._fsync_directory

        def fail_destination_fsync(directory: Path) -> None:
            if Path(directory) == document.parent and document.exists():
                raise OSError("injected destination fsync failure")
            real_fsync(directory)

        monkeypatch.setattr(phase4_exit, "_fsync_directory", fail_destination_fsync)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][0]["code"] == "recovery-write-failed"
    assert not anchor_path.exists()
    assert document.is_file()
    assert phase4_exit._identity_from_stat(document.lstat()) == expected_identity
    assert payload_path.is_file()
    assert len(_transaction_journals(docs_root)) == 2

    if failure_point == "verification":
        monkeypatch.setattr(
            phase4_exit,
            "_capture_snapshot_with_identity",
            real_capture,
        )
    else:
        monkeypatch.setattr(phase4_exit, "_fsync_directory", real_fsync)
    retry_code, retried = _run_json(capsys, root, "--recover")

    assert retry_code == phase4_exit.EXIT_OK, retried
    assert retried["recovery"]["outcome"] == "restore-finalized"
    assert phase4_exit._identity_from_stat(document.lstat()) == expected_identity
    assert not payload_path.exists()
    assert _transaction_journals(docs_root) == []


@pytest.mark.parametrize("path_state", ["both", "neither"])
def test_recover_target_created_restore_rejects_ambiguous_path_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    path_state: str,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    _crash_at(monkeypatch, "restore-target-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    if path_state == "both":
        document.write_bytes(anchor_path.read_bytes())
    else:
        anchor_path.unlink()
    journals_before = _transaction_journals(docs_root)
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert document.exists() is (path_state == "both")
    assert anchor_path.exists() is (path_state == "both")
    assert _transaction_journals(docs_root) == journals_before


def test_recover_ready_restore_requires_active_target_and_absent_staging_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)
    _crash_at(monkeypatch, "restore-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    assert document.is_file()
    assert not anchor_path.exists()
    target_identity = phase4_exit._identity_from_stat(document.lstat())
    assert target_identity != phase4_exit._identity_from_stat(payload_path.lstat())
    assert {path.name for path in _transaction_journals(docs_root)} == {
        phase4_exit._TRANSACTION_CANONICAL_FILENAME,
        phase4_exit._TRANSACTION_TARGET_FILENAME,
        phase4_exit._TRANSACTION_READY_FILENAME,
    }
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-finalized"
    assert phase4_exit._identity_from_stat(document.lstat()) == target_identity
    assert not payload_path.exists()
    assert _transaction_journals(docs_root) == []


def test_recover_restore_metadata_gap_republishes_receipt_postimage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    _crash_at(monkeypatch, "metadata-preimage-detached")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    assert document.is_file()
    assert not receipt_path.exists()
    assert len(_transaction_journals(docs_root)) == 3
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-finalized"
    assert contract.read_json_strict(receipt_path)["state"] == "restored"
    assert contract.load_manifest(docs_root)["total"] == 1
    assert _transaction_journals(docs_root) == []


def test_recover_refuses_manifest_conflict_without_touching_journal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    manifest_path = docs_root / contract.MANIFEST_FILENAME
    manifest_path.write_text('{"manual": true}\n', encoding="utf-8")
    journal_before = _transaction_journals(docs_root)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert not document.exists()
    assert _transaction_journals(docs_root) == journal_before


def test_recover_refuses_tampered_restore_target_without_overwrite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, _ = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    _crash_at(monkeypatch, "restore-copy-complete")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    document.write_text("manual recovery conflict\n", encoding="utf-8")
    journals_before = _transaction_journals(docs_root)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert document.read_text(encoding="utf-8") == "manual recovery conflict\n"
    assert _transaction_journals(docs_root) == journals_before


def test_recover_quarantine_refuses_unexpected_active_document_before_rollback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    manifest_before = (docs_root / contract.MANIFEST_FILENAME).read_bytes()
    _crash_at(monkeypatch, "quarantine-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    rogue = docs_root / "manual.md"
    _write_doc(rogue, title="并发新增文档")
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert not document.exists()
    assert rogue.is_file()
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == manifest_before
    assert len(_transaction_journals(docs_root)) == 2


def test_recover_prepared_restore_refuses_unexpected_active_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt_before = receipt_path.read_bytes()
    manifest_before = (docs_root / contract.MANIFEST_FILENAME).read_bytes()
    _crash_at(monkeypatch, "restore-copy-complete")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    rogue = docs_root / "manual.md"
    _write_doc(rogue, title="并发新增文档")
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert document.is_file()
    assert rogue.is_file()
    assert receipt_path.read_bytes() == receipt_before
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == manifest_before
    assert len(_transaction_journals(docs_root)) == 2


@pytest.mark.parametrize(
    "crash_point",
    ["restore-copy-complete", "restore-journal-ready"],
)
def test_restore_recovery_preflights_receipt_closed_set_before_metadata_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    claude_path = _write_supported_claude(root)
    apply_code, apply_payload = _run_json(capsys, root, "--apply")
    assert apply_code == phase4_exit.EXIT_OK, apply_payload
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_dir = docs_root / phase4_exit.QUARANTINE_DIRNAME / receipt_id
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    _crash_at(monkeypatch, crash_point)

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    manifest_before = manifest_path.read_bytes()
    receipt_before = receipt_path.read_bytes()
    claude_before = claude_path.read_bytes()
    foreign = receipt_dir / "manual.txt"
    foreign.write_bytes(b"manual owner\n")
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert document.is_file()
    assert foreign.read_bytes() == b"manual owner\n"
    assert manifest_path.read_bytes() == manifest_before
    assert receipt_path.read_bytes() == receipt_before
    assert claude_path.read_bytes() == claude_before
    assert _transaction_journals(docs_root)


def test_quarantine_rollback_never_overwrites_unknown_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    manual_manifest = '{"manual":true}\n'

    def conflict_commit(
        path: Path,
        value: object,
        journal: phase4_exit.TransactionJournal,
        **kwargs: object,
    ) -> None:
        del value, journal, kwargs
        contract.atomic_write_utf8(path, manual_manifest)
        raise OSError("injected concurrent manifest replacement")

    monkeypatch.setattr(phase4_exit, "_commit_manifest", conflict_commit)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-rollback-failed"
    assert contract.read_utf8(manifest_path) == manual_manifest
    assert not document.exists()
    assert len(_transaction_journals(docs_root)) == 2


def test_quarantine_rollback_preserves_unowned_staging_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    real_write_snapshot = phase4_exit._write_exclusive_snapshot
    unowned: Path | None = None

    def fail_receipt(
        path: Path,
        base: Path,
        description: str,
        snapshot: dict,
        *,
        staging_root: Path | None = None,
        staging_trusted_root: Path | None = None,
    ) -> phase4_exit.ExclusiveCopy:
        nonlocal unowned
        if description.startswith("quarantine receipt "):
            unowned = Path(path).parent / "manual.txt"
            unowned.write_text("manual\n", encoding="utf-8")
            raise OSError("injected receipt failure with unowned file")
        return real_write_snapshot(
            path,
            base,
            description,
            snapshot,
            staging_root=staging_root,
            staging_trusted_root=staging_trusted_root,
        )

    monkeypatch.setattr(phase4_exit, "_write_exclusive_snapshot", fail_receipt)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-rollback-failed"
    assert document.is_file()
    assert unowned is not None and unowned.read_text(encoding="utf-8") == "manual\n"
    assert len(_transaction_journals(docs_root)) == 1


def test_recover_without_journal_is_successful_noop(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, _ = _eligible_project(tmp_path)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["status"] == "no-pending-transaction"
    assert payload["recovery"] == {"required": False, "recovered": False}


@pytest.mark.parametrize(
    "residue_kind",
    ("prepared", "committed", "rolled_back", "temporary", "artifact-root"),
)
def test_recover_is_blocked_by_every_coordinator_pending_form(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    residue_kind: str,
) -> None:
    root, _, _ = _eligible_project(tmp_path)
    slots = contract.coordinator_publish_journal_paths(root)
    if residue_kind in slots:
        residue = slots[residue_kind]
        residue.write_bytes(b"coordinator journal residue\n")
    elif residue_kind == "temporary":
        residue = slots["prepared"].with_name(
            f"{slots['prepared'].name}{contract.COORDINATOR_TEMP_MARKER}dead.tmp"
        )
        residue.write_bytes(b"coordinator temporary residue\n")
    else:
        residue = slots["prepared"].with_name(
            f"{slots['prepared'].stem}"
            f"{contract.COORDINATOR_ARTIFACT_ROOT_MARKER}dead"
        )
        residue.mkdir()

    try:
        exit_code, payload = _run_json(capsys, root, "--recover")

        assert exit_code == phase4_exit.EXIT_REFUSED
        assert payload["status"] == "coordinator-recovery-required"
        assert payload["errors"][0]["code"] == "coordinator-recovery-required"
        assert residue.exists()
    finally:
        if residue.is_dir():
            residue.rmdir()
        else:
            residue.unlink(missing_ok=True)


def test_pending_journal_blocks_report_apply_and_restore(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, _ = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journal_data = contract.read_json_strict(_transaction_journals(docs_root)[0])
    receipt_id = journal_data["quarantine"]["moves"][0]["receipt_id"]
    for arguments in ((), ("--apply",), ("--restore", receipt_id)):
        exit_code, payload = _run_json(capsys, root, *arguments)
        assert exit_code == phase4_exit.EXIT_REFUSED
        assert payload["status"] == "phase4-recovery-required"
        assert payload["errors"][0]["code"] == "phase4-recovery-required"


@pytest.mark.parametrize("winerror", (5, 32, 33))
@pytest.mark.skipif(
    phase4_exit.os.name != "nt",
    reason="Windows os.rename no-replace path; POSIX uses renameat2",
)
def test_windows_staging_rename_retries_only_transient_locks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winerror: int,
) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    staging.mkdir()
    (staging / "payload.txt").write_bytes(b"payload\n")
    real_rename = phase4_exit.os.rename
    attempts = 0
    pauses: list[float] = []

    class WindowsBusyError(OSError):
        @property
        def winerror(self) -> int:
            return winerror

    def flaky_rename(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise WindowsBusyError("sharing violation")
        real_rename(source, destination)

    monkeypatch.setattr(phase4_exit.os, "rename", flaky_rename)
    phase4_exit._rename_staged_quarantine_directory(
        staging,
        final,
        expected_files={
            "payload.txt": phase4_exit._text_snapshot("payload\n"),
        },
        is_windows=True,
        sleep_func=pauses.append,
        max_attempts=4,
    )

    assert attempts == 3
    assert pauses == [0.01, 0.02]
    assert not staging.exists()
    assert (final / "payload.txt").read_text(encoding="utf-8") == "payload\n"


@pytest.mark.skipif(
    phase4_exit.os.name != "nt",
    reason="Windows os.rename no-replace path; POSIX uses renameat2",
)
def test_windows_staging_rename_does_not_mask_permanent_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    staging.mkdir()
    attempts = 0

    def permanent_failure(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("permanent rename failure")

    monkeypatch.setattr(phase4_exit.os, "rename", permanent_failure)
    with pytest.raises(OSError, match="permanent rename failure"):
        phase4_exit._rename_staged_quarantine_directory(
            staging,
            final,
            expected_files={},
            is_windows=True,
            sleep_func=lambda _: None,
            max_attempts=4,
        )
    assert attempts == 1


def test_initial_journal_partial_staging_never_becomes_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    real_raw_write = phase4_exit._write_raw_exclusive_snapshot

    def crash_during_staging_write(
        path: Path,
        base: Path,
        description: str,
        snapshot: dict,
    ) -> phase4_exit.ExclusiveCopy:
        if description == "transaction journal publication staging file":
            path.write_bytes(b'{"partial":')
            raise _SimulatedCrash("partial journal staging write")
        return real_raw_write(path, base, description, snapshot)

    monkeypatch.setattr(
        phase4_exit,
        "_write_raw_exclusive_snapshot",
        crash_during_staging_write,
    )
    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    assert document.is_file()
    assert _transaction_journals(docs_root) == []
    leftovers = [
        path for path in quarantine_root.iterdir() if path.name != ".phase4.lock"
    ]
    assert len(leftovers) == 1
    assert leftovers[0].name.endswith(".tmp")
    assert leftovers[0].read_bytes() == b'{"partial":'


def test_hard_exit_during_generic_staging_never_exposes_partial_canonical(
    tmp_path: Path,
) -> None:
    base = tmp_path / "publication-root"
    destination = base / "receipt" / phase4_exit.RECEIPT_FILENAME
    base.mkdir()
    payload = '{"state":"ready"}\n'
    script = r"""
import os
from pathlib import Path
import phase4_exit

base = Path(os.environ["PHASE4_TEST_BASE"])
destination = base / "receipt" / phase4_exit.RECEIPT_FILENAME


def crash(point: str) -> None:
    if point == "exclusive-staging-write-started":
        os._exit(91)


phase4_exit._transaction_fault = crash
phase4_exit._write_exclusive_snapshot(
    destination,
    base,
    "hard-exit receipt",
    phase4_exit._text_snapshot(os.environ["PHASE4_TEST_PAYLOAD"]),
)
"""
    env = os.environ.copy()
    env.update(
        {
            "PHASE4_TEST_BASE": str(base),
            "PHASE4_TEST_PAYLOAD": payload,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(Path(phase4_exit.__file__).parent),
        }
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )

    assert completed.returncode == 91, completed.stderr
    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []
    leftovers = list(base.glob(f"{phase4_exit._ARCHIVE_PREFIX}*.tmp"))
    assert len(leftovers) == 1

    phase4_exit._write_exclusive_snapshot(
        destination,
        base,
        "hard-exit receipt retry",
        phase4_exit._text_snapshot(payload),
    )
    assert destination.read_text(encoding="utf-8") == payload


def test_initial_journal_publish_is_atomic_no_replace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, document = _eligible_project(tmp_path)
    real_rename = phase4_exit._rename_no_replace
    raced_path: Path | None = None

    def racing_rename(source: Path, destination: Path) -> None:
        nonlocal raced_path
        destination = Path(destination)
        if destination.name == phase4_exit._TRANSACTION_CANONICAL_FILENAME:
            raced_path = destination
            destination.write_bytes(b"external journal owner\n")
        real_rename(source, destination)

    monkeypatch.setattr(phase4_exit, "_rename_no_replace", racing_rename)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "recovery-journal-conflict"
    assert document.is_file()
    assert raced_path is not None
    assert raced_path.read_bytes() == b"external journal owner\n"


def test_final_quarantine_validation_detects_new_inbound_reference(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _make_root(tmp_path)
    candidate = docs_root / "orphan.md"
    referrer = docs_root / "refs" / "source.md"
    _write_doc(candidate, title="退出候选")
    _write_doc(referrer, title="引用者")
    (root / "refs" / "source").mkdir(parents=True)
    _write_manifest(docs_root)
    referrer_before = referrer.read_bytes()
    real_build_inbound = phase4_exit._build_inbound_sources
    restore_after_scan = False

    def inject_reference(point: str) -> None:
        nonlocal restore_after_scan
        if point == "quarantine-before-final-validation":
            referrer.write_bytes(referrer_before + b"\n[late](../orphan.md)\n")
            restore_after_scan = True

    def scan_and_restore(
        active_paths: set[str],
        active_docs_root: Path,
    ) -> tuple[dict[str, set[str]], list[dict[str, str]]]:
        nonlocal restore_after_scan
        result = real_build_inbound(active_paths, active_docs_root)
        if restore_after_scan:
            referrer.write_bytes(referrer_before)
            restore_after_scan = False
        return result

    monkeypatch.setattr(phase4_exit, "_transaction_fault", inject_reference)
    monkeypatch.setattr(phase4_exit, "_build_inbound_sources", scan_and_restore)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "candidate-has-inbound-references"
    assert candidate.is_file()
    assert referrer.read_bytes() == referrer_before
    assert _transaction_journals(docs_root) == []


def _standalone_quarantine_move(tmp_path: Path) -> phase4_exit.QuarantineMove:
    source = tmp_path / "candidate.md"
    source.write_bytes(b"# candidate\n<!-- AUTO-GENERATED v3.0: folder-doc START -->\n")
    staging = tmp_path / "staging"
    staging_payload = staging / phase4_exit.PAYLOAD_DIRNAME / source.name
    staging_payload.parent.mkdir(parents=True)
    final_dir = tmp_path / "final"
    digest = contract.sha256_file(source)
    return phase4_exit.QuarantineMove(
        receipt={
            "original_path": source.name,
            "payload_identity": list(phase4_exit._identity_from_stat(source.lstat())),
            "sha256": digest,
        },
        source_path=source,
        staging_dir=staging,
        final_dir=final_dir,
        staging_payload=staging_payload,
        final_payload=final_dir / phase4_exit.PAYLOAD_DIRNAME / source.name,
    )


def test_source_swap_to_symlink_is_refused_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = _standalone_quarantine_move(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_bytes(move.source_path.read_bytes())

    def swap_source(point: str) -> None:
        if point == "quarantine-before-source-link":
            move.source_path.unlink()
            try:
                move.source_path.symlink_to(outside)
            except (NotImplementedError, OSError) as exc:
                pytest.skip(f"symlink creation unavailable: {exc}")

    monkeypatch.setattr(phase4_exit, "_transaction_fault", swap_source)
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._link_source_to_staging_no_replace(move)

    assert caught.value.code == "candidate-path-changed"
    assert move.source_path.is_symlink()
    assert outside.is_file()
    assert not move.staging_payload.exists()


def test_source_transfer_never_overwrites_racing_staging_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = _standalone_quarantine_move(tmp_path)

    def create_target(point: str) -> None:
        if point == "quarantine-before-source-link":
            move.staging_payload.write_bytes(b"concurrent owner\n")

    monkeypatch.setattr(phase4_exit, "_transaction_fault", create_target)
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._link_source_to_staging_no_replace(move)

    assert caught.value.code == "quarantine-staging-conflict"
    assert move.source_path.is_file()
    assert move.staging_payload.read_bytes() == b"concurrent owner\n"


@pytest.mark.skipif(
    phase4_exit.os.name != "nt",
    reason="Windows os.rename no-replace path; POSIX uses renameat2",
)
def test_windows_rename_retry_revalidates_closed_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "final"
    staging.mkdir()
    payload = staging / "payload.txt"
    payload.write_bytes(b"payload\n")
    attempts = 0

    class WindowsBusyError(OSError):
        @property
        def winerror(self) -> int:
            return 32

    def add_unowned_file_then_fail(source: Path, destination: Path) -> None:
        nonlocal attempts
        del destination
        attempts += 1
        (Path(source) / "unowned.txt").write_bytes(b"manual\n")
        raise WindowsBusyError("sharing violation")

    monkeypatch.setattr(phase4_exit.os, "rename", add_unowned_file_then_fail)
    with pytest.raises(phase4_exit.Phase4Refusal, match="unowned file"):
        phase4_exit._rename_staged_quarantine_directory(
            staging,
            final,
            expected_files={"payload.txt": phase4_exit._text_snapshot("payload\n")},
            is_windows=True,
            sleep_func=lambda _: None,
            max_attempts=4,
        )

    assert attempts == 1
    assert staging.is_dir()
    assert not final.exists()
    assert (staging / "unowned.txt").read_bytes() == b"manual\n"


def test_text_update_readback_failure_is_unknown_and_not_silently_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "metadata.txt"
    path.write_bytes(b"before\n")
    update = phase4_exit.TextUpdate(path=path, before="before\n", after="after\n")
    real_set_snapshot_state = phase4_exit._set_snapshot_state
    readback_blocked = False

    def publish_then_fail(*args: object, **kwargs: object) -> None:
        nonlocal readback_blocked
        real_set_snapshot_state(*args, **kwargs)
        readback_blocked = True
        raise OSError("post-replace durability failure")

    real_read_utf8 = phase4_exit.contract.read_utf8

    def fail_readback(target: Path) -> str:
        if readback_blocked and Path(target) == path:
            raise OSError("readback unavailable")
        return real_read_utf8(target)

    monkeypatch.setattr(phase4_exit, "_set_snapshot_state", publish_then_fail)
    monkeypatch.setattr(phase4_exit.contract, "read_utf8", fail_readback)
    with pytest.raises(OSError, match="durability failure"):
        phase4_exit._apply_text_update(update)

    assert update.state is phase4_exit.TextUpdateState.UNKNOWN
    rollback_errors = phase4_exit._rollback_text_update(update)
    assert rollback_errors
    assert update.state is phase4_exit.TextUpdateState.UNKNOWN
    assert path.read_bytes() == b"after\n"


def test_snapshot_update_refuses_replacement_after_identity_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "metadata.txt"
    path.write_text("before\n", encoding="utf-8")
    desired = phase4_exit._text_snapshot("after\n")
    before = phase4_exit._capture_snapshot(path, tmp_path, "metadata")
    real_rename = phase4_exit._rename_no_replace
    foreign_identity: tuple[int, int, int, int] | None = None
    injected = False

    def replace_then_rename(source: Path, destination: Path) -> None:
        nonlocal foreign_identity, injected
        if not injected and Path(source) == path:
            replacement = tmp_path / "foreign.tmp"
            replacement.write_text("before\n", encoding="utf-8")
            replacement.replace(path)
            foreign_identity = phase4_exit._identity_from_stat(path.lstat())
            injected = True
        real_rename(source, destination)

    monkeypatch.setattr(phase4_exit, "_rename_no_replace", replace_then_rename)
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._set_snapshot_state(
            path,
            tmp_path,
            "metadata",
            desired,
            [before],
        )

    assert caught.value.code == "recovery-conflict"
    assert path.read_text(encoding="utf-8") == "before\n"
    assert foreign_identity is not None
    assert phase4_exit._identity_from_stat(path.lstat()) == foreign_identity


def test_snapshot_update_preserves_foreign_commit_gap_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "metadata.txt"
    path.write_text("before\n", encoding="utf-8")
    before = phase4_exit._capture_snapshot(path, tmp_path, "metadata")

    def create_foreign_before_publish(point: str) -> None:
        if point == "metadata-preimage-detached":
            path.write_text("foreign\n", encoding="utf-8")

    monkeypatch.setattr(
        phase4_exit,
        "_transaction_fault",
        create_foreign_before_publish,
    )
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._set_snapshot_state(
            path,
            tmp_path,
            "metadata",
            phase4_exit._text_snapshot("after\n"),
            [before],
        )

    assert caught.value.code == "recovery-conflict"
    assert path.read_text(encoding="utf-8") == "foreign\n"


def test_restore_target_identity_swap_before_manifest_commit_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    payload_path = (
        receipt_dir / contract.read_json_strict(receipt_path)["quarantined_path"]
    )

    def replace_target(point: str) -> None:
        if point == "restore-before-final-validation":
            replacement = document.with_name("replacement.md.tmp")
            replacement.write_bytes(document.read_bytes())
            replacement.replace(document)

    monkeypatch.setattr(phase4_exit, "_transaction_fault", replace_target)
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "restore-rollback-failed"
    assert document.is_file()
    assert payload_path.is_file()
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    raw_manifest = contract.read_json_strict(docs_root / contract.MANIFEST_FILENAME)
    assert raw_manifest["total"] == 0
    assert len(_transaction_journals(docs_root)) == 3


def test_restore_postcommit_target_swap_retains_payload_and_journal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt_before = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt_before["quarantined_path"]

    def replace_target(point: str) -> None:
        if point == "restore-before-payload-cleanup":
            replacement = document.with_name("replacement.md.tmp")
            replacement.write_bytes(document.read_bytes())
            replacement.replace(document)

    monkeypatch.setattr(phase4_exit, "_transaction_fault", replace_target)
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["warnings"]
    assert "retained its payload and journal" in payload["warnings"][-1]
    assert document.is_file()
    assert payload_path.is_file()
    assert contract.read_json_strict(receipt_path)["state"] == "restored"
    assert contract.load_manifest(docs_root)["total"] == 1
    assert len(_transaction_journals(docs_root)) == 3


def test_quarantine_precommit_validator_detects_recreated_source(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    source = root / document.stem
    real_commit = phase4_exit._commit_manifest

    def recreate_source_then_commit(
        path: Path,
        value: Mapping[str, object],
        journal: phase4_exit.TransactionJournal,
        **kwargs: object,
    ) -> phase4_exit.ManifestCommitDisposition:
        source.mkdir()
        return real_commit(path, value, journal, **kwargs)

    monkeypatch.setattr(phase4_exit, "_commit_manifest", recreate_source_then_commit)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "source-no-longer-confirmed-missing"
    assert source.is_dir()
    assert document.is_file()
    assert contract.load_manifest(docs_root)["total"] == 1
    assert _transaction_journals(docs_root) == []


def test_quarantine_postcommit_validator_retains_durable_state_on_source_race(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    source = root / document.stem
    real_commit = phase4_exit._commit_manifest

    def recreate_source_after_commit(
        path: Path,
        value: Mapping[str, object],
        journal: phase4_exit.TransactionJournal,
        **kwargs: object,
    ) -> phase4_exit.ManifestCommitDisposition:
        original_postcommit = kwargs.get("postcommit_validator")
        assert callable(original_postcommit)

        def validate_after_race() -> None:
            source.mkdir()
            original_postcommit()

        kwargs["postcommit_validator"] = validate_after_race
        return real_commit(path, value, journal, **kwargs)

    monkeypatch.setattr(phase4_exit, "_commit_manifest", recreate_source_after_commit)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "manifest-postcommit-conflict"
    assert source.is_dir()
    assert not document.exists()
    assert contract.load_manifest(docs_root)["total"] == 0
    journals = _transaction_journals(docs_root)
    assert len(journals) == 2
    ready = contract.read_json_strict(
        next(
            path
            for path in journals
            if path.name == phase4_exit._TRANSACTION_READY_FILENAME
        )
    )
    receipt_id = ready["quarantine"]["moves"][0]["receipt_id"]
    receipt_dir = docs_root / phase4_exit.QUARANTINE_DIRNAME / receipt_id
    assert (receipt_dir / phase4_exit.RECEIPT_FILENAME).is_file()


def test_restore_precommit_validator_detects_target_identity_race(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    payload_path = (
        receipt_dir / contract.read_json_strict(receipt_path)["quarantined_path"]
    )
    real_commit = phase4_exit._commit_manifest
    replacement_identity: tuple[int, int, int, int] | None = None

    def replace_target_then_commit(
        path: Path,
        value: Mapping[str, object],
        journal: phase4_exit.TransactionJournal,
        **kwargs: object,
    ) -> phase4_exit.ManifestCommitDisposition:
        nonlocal replacement_identity
        replacement = document.with_name("replacement.md.tmp")
        replacement.write_bytes(document.read_bytes())
        replacement.replace(document)
        replacement_identity = phase4_exit._identity_from_stat(document.lstat())
        return real_commit(path, value, journal, **kwargs)

    monkeypatch.setattr(phase4_exit, "_commit_manifest", replace_target_then_commit)
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "restore-rollback-failed"
    assert replacement_identity is not None
    assert phase4_exit._identity_from_stat(document.lstat()) == replacement_identity
    assert payload_path.is_file()
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    raw_manifest = contract.read_json_strict(docs_root / contract.MANIFEST_FILENAME)
    assert raw_manifest["total"] == 0
    assert len(_transaction_journals(docs_root)) == 3


def test_restore_postcommit_validator_preserves_foreign_receipt_member(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt_before = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt_before["quarantined_path"]
    foreign = receipt_dir / "manual.txt"
    real_commit = phase4_exit._commit_manifest

    def add_receipt_member_after_commit(
        path: Path,
        value: Mapping[str, object],
        journal: phase4_exit.TransactionJournal,
        **kwargs: object,
    ) -> phase4_exit.ManifestCommitDisposition:
        original_postcommit = kwargs.get("postcommit_validator")
        assert callable(original_postcommit)

        def validate_after_race() -> None:
            foreign.write_bytes(b"manual owner\n")
            original_postcommit()

        kwargs["postcommit_validator"] = validate_after_race
        return real_commit(path, value, journal, **kwargs)

    monkeypatch.setattr(
        phase4_exit,
        "_commit_manifest",
        add_receipt_member_after_commit,
    )
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "manifest-postcommit-conflict"
    assert document.is_file()
    assert payload_path.is_file()
    assert foreign.read_bytes() == b"manual owner\n"
    assert contract.read_json_strict(receipt_path)["state"] == "restored"
    assert contract.load_manifest(docs_root)["total"] == 1
    assert len(_transaction_journals(docs_root)) == 3


def test_new_transactions_use_fixed_singleton_journal_slot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journals = _transaction_journals(docs_root)
    assert [path.name for path in journals] == [
        phase4_exit._TRANSACTION_CANONICAL_FILENAME
    ]
    exit_code, payload = _run_json(capsys, root, "--recover")
    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "quarantine-rolled-back"
    assert document.is_file()


def test_schema1_pending_journal_requires_audited_manual_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journal_path = _transaction_journals(docs_root)[0]
    journal = contract.read_json_strict(journal_path)
    journal["schema_version"] = 1
    journal_path.write_text(
        contract.stable_json_dumps(journal),
        encoding="utf-8",
    )
    journal_bytes = journal_path.read_bytes()
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-journal-legacy-schema"
    assert "audited manual recovery" in payload["errors"][0]["message"]
    assert document.is_file()
    assert journal_path.read_bytes() == journal_bytes


def test_legacy_random_journal_remains_recoverable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journals = _transaction_journals(docs_root)
    prepared = next(
        path
        for path in journals
        if path.name == phase4_exit._TRANSACTION_CANONICAL_FILENAME
    )
    ready = next(
        path
        for path in journals
        if path.name == phase4_exit._TRANSACTION_READY_FILENAME
    )
    data = contract.read_json_strict(ready)
    legacy = ready.with_name(
        phase4_exit._legacy_journal_filename(data["transaction_id"])
    )
    prepared.unlink()
    ready.rename(legacy)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "quarantine-rolled-back"
    assert document.is_file()
    assert _transaction_journals(docs_root) == []


def test_legacy_prepared_restore_companion_is_removed_after_recovery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(
        tmp_path,
        capsys,
    )
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    _crash_at(monkeypatch, "restore-copy-complete")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    prepared = next(
        path
        for path in _transaction_journals(docs_root)
        if path.name == phase4_exit._TRANSACTION_CANONICAL_FILENAME
    )
    data = contract.read_json_strict(prepared)
    legacy = prepared.with_name(
        phase4_exit._legacy_journal_filename(data["transaction_id"])
    )
    prepared.rename(legacy)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-finalized"
    assert document.is_file()
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    assert receipt["state"] == "restored"
    assert _transaction_journals(docs_root) == []


def test_fixed_ready_alone_precommit_state_rolls_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-ready")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journals = _transaction_journals(docs_root)
    prepared = next(
        path
        for path in journals
        if path.name == phase4_exit._TRANSACTION_CANONICAL_FILENAME
    )
    ready = next(
        path
        for path in journals
        if path.name == phase4_exit._TRANSACTION_READY_FILENAME
    )
    prepared.unlink()

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "quarantine-rolled-back"
    assert document.is_file()
    assert not ready.exists()
    assert _transaction_journals(docs_root) == []


def test_valid_publication_temp_blocks_then_promotes_on_recover(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-created")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    canonical = _transaction_journals(docs_root)[0]
    temporary = canonical.with_name(f".{canonical.name}.publish-{'a' * 32}.tmp")
    canonical.rename(temporary)
    report_code, report = _run_json(capsys, root)
    assert report_code == phase4_exit.EXIT_REFUSED
    assert report["status"] == "phase4-recovery-required"
    assert temporary.is_file()

    recover_code, recovered = _run_json(capsys, root, "--recover")
    assert recover_code == phase4_exit.EXIT_OK, recovered
    assert recovered["recovery"]["outcome"] == "quarantine-rolled-back"
    assert document.is_file()
    assert not temporary.exists()
    assert _transaction_journals(docs_root) == []


def test_partial_publication_temp_is_retained_and_blocks_all_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, _ = _eligible_project(tmp_path)
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    quarantine_root.mkdir()
    temporary = quarantine_root / (
        f".{phase4_exit._TRANSACTION_CANONICAL_FILENAME}" f".publish-{'b' * 32}.tmp"
    )
    temporary.write_bytes(b'{"partial":')

    report_code, report = _run_json(capsys, root)
    recover_code, recovered = _run_json(capsys, root, "--recover")

    assert report_code == phase4_exit.EXIT_REFUSED
    assert report["status"] == "phase4-recovery-required"
    assert recover_code == phase4_exit.EXIT_REFUSED
    assert recovered["errors"][0]["code"] == "recovery-journal-invalid"
    assert temporary.read_bytes() == b'{"partial":'


def test_closed_directory_validation_catches_member_added_after_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine_root = tmp_path / "quarantine"
    receipt_dir = quarantine_root / "receipt"
    receipt_dir.mkdir(parents=True)
    payload = receipt_dir / "payload.md"
    payload.write_bytes(b"owned\n")
    snapshot = phase4_exit._capture_snapshot(payload, receipt_dir, "payload")
    foreign = receipt_dir / "manual.txt"
    real_walk = phase4_exit.os.walk
    injected = False

    def late_injecting_walk(*args: object, **kwargs: object):
        nonlocal injected
        yield from real_walk(*args, **kwargs)
        if not injected:
            injected = True
            foreign.write_bytes(b"manual owner\n")

    monkeypatch.setattr(phase4_exit.os, "walk", late_injecting_walk)
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._require_closed_transaction_directory(
            receipt_dir,
            quarantine_root,
            {"payload.md": snapshot},
        )

    assert caught.value.code == "recovery-conflict"
    assert foreign.read_bytes() == b"manual owner\n"
    assert payload.read_bytes() == b"owned\n"


def test_foreign_receipt_member_blocks_restore_without_deletion(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, _, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    foreign = receipt_dir / "manual.txt"
    foreign.write_bytes(b"manual owner\n")

    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "recovery-conflict"
    assert foreign.read_bytes() == b"manual owner\n"


def test_quarantine_commit_rejects_same_bytes_replacement_payload_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    replacement_identity: tuple[int, int, int, int] | None = None
    replacement_path: Path | None = None

    def replace_payload_before_final_validation(point: str) -> None:
        nonlocal replacement_identity, replacement_path
        if point != "quarantine-before-final-validation":
            return
        quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
        receipt_dirs = [path for path in quarantine_root.iterdir() if path.is_dir()]
        assert len(receipt_dirs) == 1
        replacement_path = receipt_dirs[0] / phase4_exit.PAYLOAD_DIRNAME / document.name
        temporary = replacement_path.with_name("same-bytes-payload.tmp")
        temporary.write_bytes(replacement_path.read_bytes())
        temporary.replace(replacement_path)
        replacement_identity = phase4_exit._identity_from_stat(replacement_path.lstat())

    monkeypatch.setattr(
        phase4_exit,
        "_transaction_fault",
        replace_payload_before_final_validation,
    )

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-rollback-failed"
    assert not document.exists()
    assert (
        contract.read_json_strict(docs_root / contract.MANIFEST_FILENAME)["total"] == 1
    )
    assert replacement_path is not None and replacement_path.is_file()
    assert replacement_identity is not None
    assert (
        phase4_exit._identity_from_stat(replacement_path.lstat())
        == replacement_identity
    )
    assert _transaction_journals(docs_root)


def test_postcommit_recovery_rejects_foreign_receipt_member(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, _ = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-manifest-committed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journal_path = _transaction_journals(docs_root)[0]
    journal = contract.read_json_strict(journal_path)
    receipt_id = journal["quarantine"]["moves"][0]["receipt_id"]
    foreign = docs_root / phase4_exit.QUARANTINE_DIRNAME / receipt_id / "manual.txt"
    foreign.write_bytes(b"manual owner\n")
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert foreign.read_bytes() == b"manual owner\n"
    assert journal_path.is_file()


def test_postcommit_recovery_rejects_same_bytes_payload_identity_replacement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-manifest-committed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journal_path = _transaction_journals(docs_root)[0]
    journal = contract.read_json_strict(journal_path)
    move = journal["quarantine"]["moves"][0]
    payload_path = (
        docs_root
        / phase4_exit.QUARANTINE_DIRNAME
        / move["receipt_id"]
        / phase4_exit.PAYLOAD_DIRNAME
        / move["original_path"]
    )
    temporary = payload_path.with_name("same-bytes-payload.tmp")
    temporary.write_bytes(payload_path.read_bytes())
    temporary.replace(payload_path)
    replacement_identity = phase4_exit._identity_from_stat(payload_path.lstat())
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert not document.exists()
    assert payload_path.is_file()
    assert phase4_exit._identity_from_stat(payload_path.lstat()) == replacement_identity
    assert (
        contract.read_json_strict(docs_root / contract.MANIFEST_FILENAME)["total"] == 0
    )
    assert _transaction_journals(docs_root)


def test_postcommit_recovery_never_populates_foreign_receipt_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, _ = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-manifest-committed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journal_path = _transaction_journals(docs_root)[0]
    journal = contract.read_json_strict(journal_path)
    receipt_id = journal["quarantine"]["moves"][0]["receipt_id"]
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    receipt_dir = quarantine_root / receipt_id
    displaced = quarantine_root / f"{receipt_id}.owned"
    receipt_dir.rename(displaced)
    receipt_dir.mkdir()
    foreign = receipt_dir / "manual.txt"
    foreign.write_bytes(b"manual owner\n")
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "recovery-conflict"
    assert sorted(path.name for path in receipt_dir.iterdir()) == ["manual.txt"]
    assert foreign.read_bytes() == b"manual owner\n"
    assert (displaced / phase4_exit.RECEIPT_FILENAME).is_file()
    assert journal_path.is_file()


def test_visible_manifest_with_unknown_durability_retains_journal(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    _fail_at(
        monkeypatch,
        "manifest-candidate-installed",
        OSError("injected manifest parent fsync failure"),
    )

    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "manifest-durability-unknown"
    assert not document.exists()
    assert contract.read_json_strict(manifest_path)["total"] == 0
    assert len(_transaction_journals(docs_root)) == 2

    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    recover_code, recovered = _run_json(capsys, root, "--recover")
    assert recover_code == phase4_exit.EXIT_OK, recovered
    assert recovered["recovery"]["outcome"] == "quarantine-finalized"
    assert _transaction_journals(docs_root) == []


def test_recovery_rejects_same_path_journal_generation_replacement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, _ = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-manifest-committed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    selected = phase4_exit._pending_transaction(root)
    assert selected is not None
    original_identity = phase4_exit._identity_from_stat(selected.path.lstat())
    original_bytes = selected.path.read_bytes()
    real_recover = phase4_exit._recover_quarantine_transaction

    def replace_selected_generation(
        journal: phase4_exit.TransactionJournal,
        project_root: Path,
        project_docs: Path,
    ) -> str:
        outcome = real_recover(journal, project_root, project_docs)
        replacement = journal.path.with_name(f"{journal.path.name}.foreign")
        replacement.write_bytes(original_bytes)
        replacement.replace(journal.path)
        return outcome

    monkeypatch.setattr(
        phase4_exit,
        "_recover_quarantine_transaction",
        replace_selected_generation,
    )
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "recovery-journal-conflict"
    assert selected.path.read_bytes() == original_bytes
    assert phase4_exit._identity_from_stat(selected.path.lstat()) != original_identity
    assert len(_transaction_journals(docs_root)) == 2


def test_recovery_refuses_deleted_committed_manifest_without_after_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    _crash_at(monkeypatch, "manifest-candidate-installed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    assert not document.exists()
    assert contract.read_json_strict(manifest_path)["total"] == 0
    assert len(_transaction_journals(docs_root)) == 2
    manifest_path.unlink()
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "recovery-conflict"
    assert not manifest_path.exists()
    assert not document.exists()
    assert len(_transaction_journals(docs_root)) == 2
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    receipt_dirs = [
        path
        for path in quarantine_root.iterdir()
        if path.is_dir() and not path.name.startswith(phase4_exit._ARCHIVE_PREFIX)
    ]
    assert len(receipt_dirs) == 1


def test_recovery_rejects_same_bytes_with_different_restore_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, _ = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    _crash_at(monkeypatch, "restore-copy-complete")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    replacement = document.with_name("same-bytes-replacement.tmp")
    replacement.write_bytes(document.read_bytes())
    replacement.replace(document)
    journal_path = _transaction_journals(docs_root)[0]
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert document.is_file()
    assert journal_path.is_file()


def test_recovery_rejects_same_bytes_with_different_payload_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    _crash_at(monkeypatch, "restore-manifest-committed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    replacement = payload_path.with_name("same-bytes-payload.tmp")
    replacement.write_bytes(payload_path.read_bytes())
    replacement.replace(payload_path)
    replacement_identity = phase4_exit._identity_from_stat(payload_path.lstat())
    journal_paths = _transaction_journals(docs_root)
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "recovery-conflict"
    assert document.is_file()
    assert payload_path.is_file()
    assert phase4_exit._identity_from_stat(payload_path.lstat()) == replacement_identity
    assert _transaction_journals(docs_root) == journal_paths


def test_restore_journal_cleanup_failure_retains_payload_without_staged_anchor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt = contract.read_json_strict(receipt_dir / phase4_exit.RECEIPT_FILENAME)
    payload_path = receipt_dir / receipt["quarantined_path"]
    anchor_path = phase4_exit._restore_anchor_path(receipt_dir.parent, receipt_id)

    def fail_cleanup(journal: phase4_exit.TransactionJournal) -> None:
        del journal
        raise phase4_exit.Phase4OperationalError(
            "recovery-journal-cleanup-failed",
            "injected journal cleanup failure",
        )

    monkeypatch.setattr(phase4_exit, "_clear_transaction_journal", fail_cleanup)
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["warnings"]
    assert document.is_file()
    assert payload_path.is_file()
    assert not anchor_path.exists()
    target_identity = phase4_exit._identity_from_stat(document.lstat())
    payload_identity = phase4_exit._identity_from_stat(payload_path.lstat())
    assert target_identity != payload_identity
    if phase4_exit.os.name != "nt":
        retained_archives = [
            path
            for path in receipt_dir.parent.glob(f"{phase4_exit._ARCHIVE_PREFIX}*")
            if path.is_file()
        ]
        assert retained_archives
        assert all(
            phase4_exit._identity_from_stat(path.lstat()) != target_identity
            for path in retained_archives
        )
    assert len(_transaction_journals(docs_root)) == 3


@pytest.mark.skipif(
    phase4_exit.os.name != "nt",
    reason="monkeypatches os.rename, which the POSIX renameat2 path never calls",
)
def test_tombstone_removal_never_unlinks_racing_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = tmp_path / "owned.txt"
    owned.write_bytes(b"owned\n")
    expected = phase4_exit._capture_snapshot(owned, tmp_path, "owned file")
    real_rename = phase4_exit.os.rename

    def swap_before_detach(source: Path, destination: Path) -> None:
        source = Path(source)
        if source == owned:
            source.unlink()
            source.write_bytes(b"foreign\n")
        real_rename(source, destination)

    monkeypatch.setattr(phase4_exit.os, "rename", swap_before_detach)
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._remove_snapshot_file(
            owned,
            tmp_path,
            "owned file",
            expected,
        )

    assert caught.value.code == "recovery-conflict"
    assert owned.read_bytes() == b"foreign\n"
    assert not list(tmp_path.glob(f"{phase4_exit._DETACHED_PREFIX}*"))


def test_handle_bound_snapshot_rejects_pathname_aba_content_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.txt"
    displaced = tmp_path / "displaced.txt"
    replacement = tmp_path / "replacement.txt"
    target.write_bytes(b"owned\n")
    replacement.write_bytes(b"foreign\n")
    real_read = phase4_exit._read_stable_regular_fd

    def pathname_aba(fd: int, path: Path) -> tuple[bytes, os.stat_result]:
        target.rename(displaced)
        replacement.rename(target)
        try:
            return real_read(fd, path)
        finally:
            target.rename(replacement)
            displaced.rename(target)

    monkeypatch.setattr(phase4_exit, "_read_stable_regular_fd", pathname_aba)
    snapshot, identity = phase4_exit._capture_snapshot_with_identity(
        target,
        tmp_path,
        "ABA target",
    )

    assert snapshot == phase4_exit._text_snapshot("owned\n")
    assert identity == phase4_exit._identity_from_stat(target.lstat())
    assert replacement.read_bytes() == b"foreign\n"


def test_identity_bound_detach_rejects_in_place_content_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = tmp_path / "owned.txt"
    owned.write_bytes(b"owned\n")
    expected = phase4_exit._capture_snapshot(owned, tmp_path, "owned file")
    real_rename = phase4_exit._rename_no_replace

    def mutate_before_detach(source: Path, destination: Path) -> None:
        if Path(source) == owned:
            owned.write_bytes(b"foreign in-place edit\n")
        real_rename(source, destination)

    monkeypatch.setattr(phase4_exit, "_rename_no_replace", mutate_before_detach)
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._remove_snapshot_file(
            owned,
            tmp_path,
            "owned file",
            expected,
        )

    assert caught.value.code == "recovery-conflict"
    assert owned.read_bytes() == b"foreign in-place edit\n"
    assert not list(tmp_path.glob(f"{phase4_exit._DETACHED_PREFIX}*"))


def test_restore_in_place_target_write_preserves_quarantine_payload(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt["quarantined_path"]
    payload_before = payload_path.read_bytes()

    def mutate_target(point: str) -> None:
        if point == "restore-before-final-validation":
            document.write_text("concurrent in-place edit\n", encoding="utf-8")

    monkeypatch.setattr(phase4_exit, "_transaction_fault", mutate_target)
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "restore-rollback-failed"
    assert document.read_text(encoding="utf-8") == "concurrent in-place edit\n"
    assert payload_path.read_bytes() == payload_before
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    assert (
        contract.read_json_strict(docs_root / contract.MANIFEST_FILENAME)["total"] == 0
    )
    assert _transaction_journals(docs_root)

    recover_code, recover_payload = _run_json(capsys, root, "--recover")
    assert recover_code == phase4_exit.EXIT_REFUSED
    assert recover_payload["errors"][-1]["code"] == "recovery-conflict"
    assert document.read_text(encoding="utf-8") == "concurrent in-place edit\n"
    assert payload_path.read_bytes() == payload_before
    assert _transaction_journals(docs_root)


def test_ready_companion_race_is_never_overwritten(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    ready_path = quarantine_root / phase4_exit._TRANSACTION_READY_FILENAME
    foreign = b'{"foreign":true}\n'
    real_publish = phase4_exit._publish_exclusive_snapshot

    def race_ready_publication(
        path: Path,
        base: Path,
        description: str,
        snapshot: object,
        *,
        validator: object | None = None,
        staging_root: Path | None = None,
        staging_trusted_root: Path | None = None,
    ) -> phase4_exit.ExclusiveCopy:
        if description == "transaction journal ready companion":
            path.write_bytes(foreign)
        return real_publish(
            path,
            base,
            description,
            snapshot,
            validator=validator,
            staging_root=staging_root,
            staging_trusted_root=staging_trusted_root,
        )

    monkeypatch.setattr(
        phase4_exit,
        "_publish_exclusive_snapshot",
        race_ready_publication,
    )
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-rollback-failed"
    assert document.is_file()
    assert ready_path.read_bytes() == foreign
    assert len(_transaction_journals(docs_root)) == 2


def test_manifest_gap_race_preserves_foreign_canonical(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    foreign = '{"manual":true}\n'

    def fill_manifest_gap(point: str) -> None:
        if point == "manifest-before-detached":
            manifest_path.write_text(foreign, encoding="utf-8")

    monkeypatch.setattr(phase4_exit, "_transaction_fault", fill_manifest_gap)
    exit_code, payload = _run_json(capsys, root, "--apply")

    assert exit_code == phase4_exit.EXIT_OPERATIONAL_FAILURE
    assert payload["errors"][-1]["code"] == "quarantine-rollback-failed"
    assert manifest_path.read_text(encoding="utf-8") == foreign
    assert not document.exists()
    assert len(_transaction_journals(docs_root)) == 2


@pytest.mark.parametrize(
    ("fault_point", "expected_outcome", "document_restored"),
    [
        ("manifest-before-anchor-staged", "quarantine-rolled-back", True),
        ("manifest-candidate-staged", "quarantine-rolled-back", True),
        ("manifest-before-detached", "quarantine-rolled-back", True),
        ("manifest-candidate-installed", "quarantine-finalized", False),
    ],
)
def test_manifest_cas_crash_points_are_recoverable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
    expected_outcome: str,
    document_restored: bool,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    _crash_at(monkeypatch, fault_point)

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    assert len(_transaction_journals(docs_root)) == 2
    if fault_point == "manifest-before-detached":
        assert not manifest_path.exists()
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == expected_outcome
    assert document.exists() is document_restored
    assert contract.load_manifest(docs_root)["total"] == int(document_restored)
    assert _transaction_journals(docs_root) == []
    assert not list(docs_root.glob(".phase4-manifest-*.tmp"))

    second_code, second_payload = _run_json(capsys, root, "--recover")
    assert second_code == phase4_exit.EXIT_OK
    assert second_payload["status"] == "no-pending-transaction"
    assert second_payload["recovery"] == {"required": False, "recovered": False}


def test_ready_alone_cleanup_residue_recovers_quarantine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    prepared_path = quarantine_root / phase4_exit._TRANSACTION_CANONICAL_FILENAME
    ready_path = quarantine_root / phase4_exit._TRANSACTION_READY_FILENAME
    _crash_at(monkeypatch, "journal-prepared-removed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    assert not prepared_path.exists()
    assert ready_path.is_file()
    assert not document.exists()
    assert contract.load_manifest(docs_root)["total"] == 0

    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "quarantine-finalized"
    assert not ready_path.exists()
    assert _transaction_journals(docs_root) == []


def test_ready_alone_cleanup_residue_recovers_restore(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(
        tmp_path,
        capsys,
    )
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt["quarantined_path"]
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    prepared_path = quarantine_root / phase4_exit._TRANSACTION_CANONICAL_FILENAME
    target_path = quarantine_root / phase4_exit._TRANSACTION_TARGET_FILENAME
    ready_path = quarantine_root / phase4_exit._TRANSACTION_READY_FILENAME
    anchor_path = phase4_exit._restore_anchor_path(quarantine_root, receipt_id)
    _crash_at(monkeypatch, "journal-prepared-removed")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    assert not prepared_path.exists()
    assert target_path.is_file()
    assert ready_path.is_file()
    assert document.is_file()
    assert payload_path.is_file()
    assert not anchor_path.exists()
    assert contract.load_manifest(docs_root)["total"] == 1

    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_OK, payload
    assert payload["recovery"]["outcome"] == "restore-finalized"
    assert contract.read_json_strict(receipt_path)["state"] == "restored"
    assert document.is_file()
    assert not payload_path.exists()
    assert not anchor_path.exists()
    assert not target_path.exists()
    assert not ready_path.exists()
    assert _transaction_journals(docs_root) == []


def test_final_restore_validation_rejects_foreign_receipt_member(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_path = receipt_dir / phase4_exit.RECEIPT_FILENAME
    receipt = contract.read_json_strict(receipt_path)
    payload_path = receipt_dir / receipt["quarantined_path"]
    foreign = receipt_dir / "manual.txt"

    def add_foreign_member(point: str) -> None:
        if point == "restore-before-final-validation":
            foreign.write_bytes(b"manual owner\n")

    monkeypatch.setattr(phase4_exit, "_transaction_fault", add_foreign_member)
    exit_code, payload = _run_json(capsys, root, "--restore", receipt_id)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][-1]["code"] == "recovery-conflict"
    assert not document.exists()
    assert foreign.read_bytes() == b"manual owner\n"
    assert payload_path.is_file()
    assert contract.read_json_strict(receipt_path)["state"] == "quarantined"
    assert contract.load_manifest(docs_root)["total"] == 0
    assert _transaction_journals(docs_root) == []


@pytest.mark.skipif(phase4_exit.os.name != "nt", reason="Windows directory API")
def test_windows_directory_fsync_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phase4_exit._fsync_directory(tmp_path)
    expected_identity = phase4_exit._identity_from_stat(tmp_path.lstat())
    calls: list[tuple[Path, tuple[int, int, int, int]]] = []

    def fail_flush(
        directory: Path,
        identity: tuple[int, int, int, int],
    ) -> None:
        calls.append((Path(directory), identity))
        raise OSError("injected directory flush failure")

    monkeypatch.setattr(phase4_exit, "_fsync_directory_windows", fail_flush)
    with pytest.raises(OSError, match="injected directory flush failure"):
        phase4_exit._fsync_directory(tmp_path)

    assert calls == [(tmp_path, expected_identity)]


@pytest.mark.skipif(phase4_exit.os.name != "nt", reason="Windows handle deletion")
def test_handle_bound_delete_never_deletes_tombstone_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = tmp_path / "owned.txt"
    owned.write_bytes(b"owned\n")
    expected = phase4_exit._capture_snapshot(owned, tmp_path, "owned file")
    real_delete = phase4_exit._delete_regular_file_by_handle_windows
    replacements: list[tuple[Path, Path]] = []

    def swap_detached_file(
        path: Path,
        identity: tuple[int, int, int, int],
        snapshot: Mapping[str, object],
    ) -> None:
        archived = path.with_name(f"{path.name}.owned")
        phase4_exit._rename_no_replace(path, archived)
        path.write_bytes(b"foreign\n")
        replacements.append((path, archived))
        real_delete(path, identity, snapshot)

    monkeypatch.setattr(
        phase4_exit,
        "_delete_regular_file_by_handle_windows",
        swap_detached_file,
    )
    with pytest.raises(phase4_exit.Phase4OperationalError):
        phase4_exit._remove_snapshot_file(
            owned,
            tmp_path,
            "owned file",
            expected,
        )

    foreign_path, archived_path = replacements[0]
    assert foreign_path.read_bytes() == b"foreign\n"
    assert archived_path.read_bytes() == b"owned\n"
    assert not owned.exists()


@pytest.mark.skipif(phase4_exit.os.name != "nt", reason="Windows handle deletion")
def test_handle_delete_rechecks_content_on_deletion_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = tmp_path / "owned.txt"
    owned.write_bytes(b"owned\n")
    expected = phase4_exit._capture_snapshot(owned, tmp_path, "owned file")
    real_delete = phase4_exit._delete_regular_file_by_handle_windows
    tombstones: list[Path] = []

    def mutate_detached_inode(
        path: Path,
        identity: tuple[int, int, int, int],
        snapshot: Mapping[str, object],
    ) -> None:
        path.write_bytes(b"foreign in-place edit\n")
        tombstones.append(path)
        real_delete(path, identity, snapshot)

    monkeypatch.setattr(
        phase4_exit,
        "_delete_regular_file_by_handle_windows",
        mutate_detached_inode,
    )
    with pytest.raises(phase4_exit.Phase4OperationalError):
        phase4_exit._remove_snapshot_file(
            owned,
            tmp_path,
            "owned file",
            expected,
        )

    assert not owned.exists()
    assert len(tombstones) == 1
    assert tombstones[0].read_bytes() == b"foreign in-place edit\n"


@pytest.mark.skipif(phase4_exit.os.name != "nt", reason="Windows handle deletion")
def test_detached_journal_cleanup_failure_blocks_later_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, _ = _eligible_project(tmp_path)
    _crash_at(monkeypatch, "quarantine-journal-created")
    with pytest.raises(_SimulatedCrash):
        phase4_exit.apply_quarantine(root)
    journal = phase4_exit._pending_transaction(root)
    assert journal is not None

    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)

    def fail_delete(
        path: Path,
        identity: tuple[int, int, int, int],
        snapshot: Mapping[str, object],
    ) -> None:
        del path, identity, snapshot
        raise OSError("injected handle deletion failure")

    monkeypatch.setattr(
        phase4_exit,
        "_delete_regular_file_by_handle_windows",
        fail_delete,
    )
    with pytest.raises(phase4_exit.Phase4OperationalError):
        phase4_exit._clear_transaction_journal(journal)

    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    detached = list(quarantine_root.glob(f"{phase4_exit._DETACHED_PREFIX}*"))
    assert len(detached) == 1
    exit_code, payload = _run_json(capsys, root)
    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["status"] == "phase4-recovery-required"
    assert detached[0].is_file()


@pytest.mark.skipif(phase4_exit.os.name != "nt", reason="Windows handle deletion")
def test_handle_bound_directory_delete_retains_foreign_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine_root = tmp_path / "quarantine"
    receipt_dir = quarantine_root / "receipt"
    payload_dir = receipt_dir / "document"
    payload_dir.mkdir(parents=True)
    payload = payload_dir / "orphan.md"
    payload.write_text("owned\n", encoding="utf-8")
    snapshot = phase4_exit._capture_snapshot(payload, receipt_dir, "payload")
    real_delete = phase4_exit._delete_empty_directory_by_handle_windows
    replacements: list[tuple[Path, Path]] = []

    def swap_directory(
        path: Path,
        identity: tuple[int, int, int, int],
    ) -> None:
        if path.name == "document":
            archived = path.with_name("document.owned")
            phase4_exit._rename_no_replace(path, archived)
            path.mkdir()
            replacements.append((path, archived))
        real_delete(path, identity)

    monkeypatch.setattr(
        phase4_exit,
        "_delete_empty_directory_by_handle_windows",
        swap_directory,
    )
    with pytest.raises(phase4_exit.Phase4Refusal) as caught:
        phase4_exit._safe_remove_transaction_directory(
            receipt_dir,
            quarantine_root,
            {"document/orphan.md": snapshot},
        )

    assert caught.value.code == "recovery-conflict"
    foreign_path, archived_path = replacements[0]
    assert foreign_path.is_dir()
    assert archived_path.is_dir()
    assert not receipt_dir.exists()


def test_posix_file_archive_is_moved_outside_receipt_closed_set(
    tmp_path: Path,
) -> None:
    quarantine_root = tmp_path / "quarantine"
    payload_dir = quarantine_root / "receipt" / phase4_exit.PAYLOAD_DIRNAME
    payload_dir.mkdir(parents=True)
    payload = payload_dir / "orphan.md"
    payload.write_text("owned\n", encoding="utf-8")
    identity = phase4_exit._identity_from_stat(payload.lstat())

    phase4_exit._detach_and_remove_regular_file(
        payload,
        identity,
        "restored duplicate payload",
        archive_root=quarantine_root,
        is_windows=False,
    )

    assert not payload.exists()
    assert list(payload_dir.glob(f"{phase4_exit._ARCHIVE_PREFIX}*")) == []
    archives = list(quarantine_root.glob(f"{phase4_exit._ARCHIVE_PREFIX}*.tmp"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "owned\n"


def test_restore_target_is_independent_from_payload_and_retained_archive(
    tmp_path: Path,
) -> None:
    docs_root = tmp_path / "project_docs"
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    receipt_dir = quarantine_root / "receipt"
    receipt_dir.mkdir(parents=True)
    payload = receipt_dir / "orphan.md"
    payload.write_text("owned restore bytes\n", encoding="utf-8")
    payload_snapshot, payload_identity = phase4_exit._capture_snapshot_with_identity(
        payload,
        docs_root,
        "restore payload",
    )
    assert payload_identity is not None

    anchor = quarantine_root / f"{phase4_exit._RESTORE_ANCHOR_PREFIX}test.tmp"
    staged = phase4_exit._write_exclusive_snapshot(
        anchor,
        quarantine_root,
        "restore staged target",
        payload_snapshot,
    )
    assert staged.identity != payload_identity
    target = docs_root / "orphan.md"
    created = phase4_exit._exclusive_copy_file(
        anchor,
        target,
        payload_snapshot["sha256"],
        staged.identity,
        archive_root=quarantine_root,
        archive_trusted_root=docs_root,
    )

    target_identity = phase4_exit._identity_from_stat(target.lstat())
    assert created.identity == target_identity
    assert target_identity != payload_identity
    assert not anchor.exists()
    target_bytes = target.read_bytes()
    target_digest = contract.sha256_file(target)

    phase4_exit._detach_and_remove_regular_file(
        payload,
        payload_identity,
        "restored duplicate payload",
        expected_snapshot=payload_snapshot,
        archive_root=quarantine_root,
        archive_trusted_root=docs_root,
        is_windows=False,
    )

    archives = list(quarantine_root.glob(f"{phase4_exit._ARCHIVE_PREFIX}*.tmp"))
    assert len(archives) == 1
    assert phase4_exit._identity_from_stat(archives[0].lstat()) == payload_identity
    assert phase4_exit._identity_from_stat(archives[0].lstat()) != target_identity
    archives[0].write_bytes(b"mutated retained archive\n")
    assert target.read_bytes() == target_bytes
    assert contract.sha256_file(target) == target_digest


def test_report_rejects_retained_archive_aliasing_active_manifest_document(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document = _eligible_project(tmp_path)
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    quarantine_root.mkdir()
    archive = quarantine_root / f"{phase4_exit._ARCHIVE_PREFIX}legacy.tmp"
    try:
        os.link(document, archive)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    exit_code, payload = _run_json(capsys, root)

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "phase4-archive-active-alias"
    assert document.is_file()
    assert archive.is_file()
    assert phase4_exit._identity_from_stat(document.lstat())[:2] == (
        phase4_exit._identity_from_stat(archive.lstat())[:2]
    )


def test_recovery_rejects_retained_archive_aliasing_restore_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(tmp_path, capsys)
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    quarantine_root = receipt_dir.parent
    _crash_at(monkeypatch, "restore-copy-complete")

    with pytest.raises(_SimulatedCrash):
        phase4_exit.restore_receipt(root, receipt_id)

    archive = quarantine_root / f"{phase4_exit._ARCHIVE_PREFIX}legacy-target.tmp"
    try:
        os.link(document, archive)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    journals_before = _transaction_journals(docs_root)
    monkeypatch.setattr(phase4_exit, "_transaction_fault", lambda point: None)
    exit_code, payload = _run_json(capsys, root, "--recover")

    assert exit_code == phase4_exit.EXIT_REFUSED
    assert payload["errors"][0]["code"] == "phase4-archive-active-alias"
    assert document.is_file()
    assert archive.is_file()
    assert _transaction_journals(docs_root) == journals_before


def test_posix_archive_accepts_sibling_source_under_shared_trusted_root(
    tmp_path: Path,
) -> None:
    docs_root = tmp_path / "project_docs"
    source_dir = docs_root / "subsystem"
    quarantine_root = docs_root / phase4_exit.QUARANTINE_DIRNAME
    source_dir.mkdir(parents=True)
    quarantine_root.mkdir()
    source = source_dir / "orphan.md"
    source.write_text("owned\n", encoding="utf-8")
    identity = phase4_exit._identity_from_stat(source.lstat())

    phase4_exit._detach_and_remove_regular_file(
        source,
        identity,
        "quarantine source",
        archive_root=quarantine_root,
        archive_trusted_root=docs_root,
        is_windows=False,
    )

    assert not source.exists()
    archives = list(quarantine_root.glob(f"{phase4_exit._ARCHIVE_PREFIX}*.tmp"))
    assert len(archives) == 1
    assert archives[0].read_text(encoding="utf-8") == "owned\n"


@pytest.mark.skipif(
    phase4_exit.sys.platform not in ("linux", "darwin"),
    reason="POSIX no-replace archive policy",
)
def test_posix_apply_restore_and_repeat_restore_are_idempotent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, docs_root, document, apply_payload, receipt_dir = _apply_one(
        tmp_path,
        capsys,
    )
    receipt_id = apply_payload["quarantined"][0]["receipt_id"]
    receipt_before = contract.read_json_strict(
        receipt_dir / phase4_exit.RECEIPT_FILENAME
    )

    restore_code, restored = _run_json(capsys, root, "--restore", receipt_id)
    repeat_code, repeated = _run_json(capsys, root, "--restore", receipt_id)

    assert restore_code == phase4_exit.EXIT_OK, restored
    assert repeat_code == phase4_exit.EXIT_OK, repeated
    assert repeated["restored"]["no_op"] is True
    assert document.is_file()
    assert contract.sha256_file(document) == receipt_before["sha256"]
    assert not (receipt_dir / receipt_before["quarantined_path"]).exists()
    assert _transaction_journals(docs_root) == []
    assert list(receipt_dir.rglob(f"{phase4_exit._ARCHIVE_PREFIX}*")) == []
    assert list(
        (docs_root / phase4_exit.QUARANTINE_DIRNAME).glob(
            f"{phase4_exit._ARCHIVE_PREFIX}*"
        )
    )


@pytest.mark.skipif(
    phase4_exit.sys.platform not in ("linux", "darwin"),
    reason="POSIX no-replace archive policy",
)
def test_posix_transaction_directory_cleanup_retains_closed_archive(
    tmp_path: Path,
) -> None:
    quarantine_root = tmp_path / "quarantine"
    receipt_dir = quarantine_root / "receipt"
    payload_dir = receipt_dir / "document"
    payload_dir.mkdir(parents=True)
    payload = payload_dir / "orphan.md"
    payload.write_text("owned\n", encoding="utf-8")
    snapshot = phase4_exit._capture_snapshot(payload, receipt_dir, "payload")

    phase4_exit._safe_remove_transaction_directory(
        receipt_dir,
        quarantine_root,
        {"document/orphan.md": snapshot},
    )

    assert not receipt_dir.exists()
    archives = list(quarantine_root.glob(f"{phase4_exit._ARCHIVE_PREFIX}*.dir"))
    assert len(archives) == 1
    assert (archives[0] / "document" / "orphan.md").read_text(
        encoding="utf-8"
    ) == "owned\n"


def test_rename_no_replace_never_replaces_existing_destination(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source\n", encoding="utf-8")
    destination.write_text("destination\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        phase4_exit._rename_no_replace(source, destination)

    assert source.read_text(encoding="utf-8") == "source\n"
    assert destination.read_text(encoding="utf-8") == "destination\n"


@pytest.mark.parametrize(
    "unsupported_errno",
    (
        phase4_exit.errno.ENOSYS,
        phase4_exit.errno.EINVAL,
        phase4_exit.errno.ENOTSUP,
        getattr(phase4_exit.errno, "EOPNOTSUPP", phase4_exit.errno.ENOTSUP),
    ),
)
def test_rename_errno_maps_unsupported_filesystem_to_typed_error(
    tmp_path: Path,
    unsupported_errno: int,
) -> None:
    destination = tmp_path / "destination.txt"

    with pytest.raises(phase4_exit.NoReplaceRenameUnsupported) as caught:
        phase4_exit._raise_rename_errno(unsupported_errno, destination)

    message = str(caught.value)
    assert "refusing to fall back to a replacing rename" in message
    assert caught.value.errno == phase4_exit.errno.ENOTSUP


def test_rename_errno_keeps_conflict_and_plain_failures_distinct(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination.txt"

    with pytest.raises(FileExistsError):
        phase4_exit._raise_rename_errno(phase4_exit.errno.EEXIST, destination)

    with pytest.raises(OSError) as caught:
        phase4_exit._raise_rename_errno(phase4_exit.errno.EACCES, destination)

    assert not isinstance(caught.value, phase4_exit.NoReplaceRenameUnsupported)


@pytest.mark.skipif(
    phase4_exit.os.name != "posix",
    reason="POSIX directory fsync branch",
)
def test_posix_directory_fsync_degrades_when_filesystem_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "flushed"
    directory.mkdir()
    monkeypatch.setattr(phase4_exit, "_DEGRADED_DIRECTORY_FSYNC", [])

    def refuse(fd: int) -> None:
        del fd
        raise OSError(phase4_exit.errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(phase4_exit.os, "fsync", refuse)

    with pytest.warns(RuntimeWarning, match="durability is degraded"):
        phase4_exit._fsync_directory(directory)

    degraded = phase4_exit.directory_fsync_degraded()
    assert len(degraded) == 1
    assert str(directory) in degraded[0]


@pytest.mark.skipif(
    phase4_exit.os.name != "posix",
    reason="POSIX directory fsync branch",
)
def test_posix_directory_fsync_propagates_real_io_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "flushed"
    directory.mkdir()
    monkeypatch.setattr(phase4_exit, "_DEGRADED_DIRECTORY_FSYNC", [])

    def fail(fd: int) -> None:
        del fd
        raise OSError(phase4_exit.errno.EIO, "Input/output error")

    monkeypatch.setattr(phase4_exit.os, "fsync", fail)

    with pytest.raises(OSError) as caught:
        phase4_exit._fsync_directory(directory)

    assert caught.value.errno == phase4_exit.errno.EIO
    assert phase4_exit.directory_fsync_degraded() == ()


@pytest.mark.skipif(
    phase4_exit.os.name != "posix",
    reason="POSIX directory fsync branch",
)
def test_posix_directory_fsync_success_path_is_silent(tmp_path: Path) -> None:
    directory = tmp_path / "flushed"
    directory.mkdir()

    phase4_exit._fsync_directory(directory)
