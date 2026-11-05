# -*- coding: utf-8 -*-
"""同步协调器的临时项目事务与幂等性测试。"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import claude_doc_index
import phase4_exit
import project_docs_contract as contract
import scan_dirs
import sync_project_docs as coordinator

SYNCED = "2026-07-30T00:00:00+00:00"
# 可选的外部 reader 技能（默认位置 .dsh/skills/load-project）；未安装时对应用例跳过。
LOAD_TOOL = (
    Path(__file__).resolve().parents[2] / "load-project" / "tools" / "load_project.py"
)
SYNC_TOOL = Path(__file__).resolve().parents[1] / "tools" / "sync_project_docs.py"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _claude() -> str:
    lines = ["# CLAUDE.md", "", "manual-before"]
    for section in claude_doc_index.REQUIRED_SECTIONS:
        lines.extend(
            [
                claude_doc_index.section_start(section),
                f"old-{section}",
                claude_doc_index.section_end(section),
                f"manual-after-{section}",
            ]
        )
    return "\n".join(lines) + "\n"


def _placeholder_document(
    *,
    broken_link: bool = False,
    corrupt_marker: bool = False,
) -> str:
    optional_link = "\n[缺失文档](missing.md)\n" if broken_link else ""
    marker_tail = " v3.1 -->" if corrupt_marker else ""
    return f"""# module — 占位模块

<!-- DOC-PATH: project_docs/module.md -->
<!-- AUTO-GENERATED v3.0: folder-doc START -->
<!-- Last synced: {SYNCED} by proj-doc-builder v4.0 -->{marker_tail}

**导航**: **module**

## 概述

本目录当前为占位。待后续实现。

## 文件清单

| 文件 | 用途 |
|------|------|
| `__init__.py` | 包初始化文件 |

## 系统角色

本目录预留模块入口，后续实现将由上层组件调用。
{optional_link}
## 状态

⬜ 占位 — 目录已建立，待实现。

<!-- AUTO-GENERATED v3.0: folder-doc END -->
"""


def _project(
    tmp_path: Path,
    *,
    broken_link: bool = False,
    corrupt_marker: bool = False,
) -> tuple[Path, Path]:
    root = tmp_path / "project"
    docs_root = root / "project_docs"
    source = root / "module"
    source.mkdir(parents=True)
    _write(source / "__init__.py", "# -*- coding: utf-8 -*-\n")
    _write(root / "CLAUDE.md", _claude())
    _write(
        docs_root / "module.md",
        _placeholder_document(
            broken_link=broken_link,
            corrupt_marker=corrupt_marker,
        ),
    )
    return root, docs_root


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _tree_paths(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


def test_indirect_project_root_is_rejected(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    root_link = tmp_path / "project-link"
    try:
        root_link.symlink_to(root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(coordinator.SyncInputError, match="direct real directory"):
        coordinator.synchronize_project(root_link, run_timestamp=SYNCED)


def test_dry_run_passes_all_gates_without_writing(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    before = _file_bytes(root)

    report, exit_code = coordinator.synchronize_project(
        root,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["ok"] is True
    assert report["mode"] == "dry-run"
    assert report["manifest"]["schema_version"] == 2
    assert report["scan"]["changes"]["new"] == ["", "module"]
    assert [item["path"] for item in report["scan"]["worklist"]["directories"]] == [
        "module",
        "",
    ]
    scaffold = report["scan"]["worklist"]["scaffold"]
    assert len(scaffold) == 2
    assert scaffold[0]["path"] == "module"
    assert scaffold[0]["change"] == "new"
    assert scaffold[0]["doc_path"] == "project_docs/module.md"
    assert scaffold[0]["recommended_profile"] == "placeholder"
    assert "precomputed_links" in scaffold[0]
    assert scaffold[1]["path"] == ""
    assert scaffold[1]["change"] == "new"
    assert scaffold[1]["doc_path"] == "project_docs/index.md"
    assert report["links"]["broken_count"] == 0
    assert report["publish"]["requested"] is False
    assert _file_bytes(root) == before


def test_dry_run_stages_outside_project_and_removes_stage_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    original_prepare = coordinator._prepare
    observed_stage_roots: list[Path] = []

    def record_stage(paths, stage_docs, **kwargs):
        stage_root = stage_docs.parent.resolve(strict=True)
        observed_stage_roots.append(stage_root)
        assert not stage_root.is_relative_to(root.resolve())
        return original_prepare(paths, stage_docs, **kwargs)

    monkeypatch.setattr(coordinator, "_prepare", record_stage)

    report, exit_code = coordinator.synchronize_project(
        root,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["ok"] is True
    assert len(observed_stage_roots) == 1
    assert not observed_stage_roots[0].exists()


def test_stage_cleanup_failure_is_not_silently_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    original_rmtree = shutil.rmtree
    staged: list[Path] = []

    def fail_cleanup(path: str | os.PathLike[str]) -> None:
        staged.append(Path(path))
        raise OSError("injected staging cleanup failure")

    monkeypatch.setattr(coordinator.shutil, "rmtree", fail_cleanup)
    try:
        with pytest.raises(
            coordinator.SyncInputError,
            match="cannot clean coordinator staging directory",
        ):
            coordinator.synchronize_project(root, run_timestamp=SYNCED)
    finally:
        monkeypatch.setattr(coordinator.shutil, "rmtree", original_rmtree)
        for path in staged:
            if path.exists():
                original_rmtree(path)

    assert len(staged) == 1


def test_cli_dry_run_without_bytecode_env_writes_no_project_paths_or_cache(
    tmp_path: Path,
) -> None:
    root, _ = _project(tmp_path / "target")
    copied_tools = tmp_path / "standalone-skill" / "tools"
    shutil.copytree(
        SYNC_TOOL.parent,
        copied_tools,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    copied_cli = copied_tools / SYNC_TOOL.name
    before_paths = _tree_paths(root)
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment["PYTHONIOENCODING"] = "utf-8"

    completed = subprocess.run(
        [
            sys.executable,
            str(copied_cli),
            str(root),
            "--dry-run",
            "--json",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == coordinator.EXIT_OK, completed.stdout
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["ok"] is True
    assert _tree_paths(root) == before_paths
    assert not list(copied_tools.rglob("__pycache__"))
    assert not list(copied_tools.rglob("*.py[co]"))


def test_apply_publishes_v2_state_and_second_run_is_noop(tmp_path: Path) -> None:
    root, docs_root = _project(tmp_path)

    first, first_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )

    assert first_code == coordinator.EXIT_OK
    assert first["publish"]["completed"] is True
    assert first["publish"]["manifest_commit_point"] is True
    assert first["publish"]["scan_state_committed"] is True
    manifest = contract.load_manifest(docs_root, verify_files=True)
    assert manifest["schema_version"] == 2
    assert manifest["total"] == 1
    assert (docs_root / coordinator.SCAN_STATE_FILENAME).is_file()
    assert "old-doc-tree-instruction" not in (root / "CLAUDE.md").read_text(
        encoding="utf-8"
    )

    committed = _file_bytes(root)
    second, second_code = coordinator.synchronize_project(
        root,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert second_code == coordinator.EXIT_OK
    assert second["planned_writes"] == []
    assert second["manifest"]["changed"] is False
    assert second["scan"]["commit_needed"] is False
    assert _file_bytes(root) == committed


@pytest.mark.skipif(
    not LOAD_TOOL.is_file(),
    reason="optional 'load-project' skill is not installed; reader integration skipped",
)
def test_applied_project_loads_overview_and_target_without_writes(
    tmp_path: Path,
) -> None:
    root, _ = _project(tmp_path)
    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert exit_code == coordinator.EXIT_OK
    assert report["publish"]["completed"] is True
    before_load = _file_bytes(root)
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    for mode_args, expected_mode in (
        (["--mode", "overview"], "overview"),
        (["--target", "module"], "target"),
    ):
        completed = subprocess.run(
            [
                sys.executable,
                str(LOAD_TOOL),
                "--project-root",
                str(root),
                *mode_args,
            ],
            check=False,
            capture_output=True,
            env=environment,
        )
        assert completed.returncode == 0, completed.stdout.decode("utf-8")
        assert completed.stderr == b""
        payload = json.loads(completed.stdout.decode("utf-8"))
        assert payload["ok"] is True
        assert payload["mode"] == expected_mode
        assert payload["manifest"]["compatibility"] == "v2_verified"
        assert payload["selected"]["count"] == 1
        assert payload["omitted"]["count"] == 0

    assert _file_bytes(root) == before_load


def test_broken_link_blocks_apply_without_any_publish(tmp_path: Path) -> None:
    root, docs_root = _project(tmp_path, broken_link=True)
    before = _file_bytes(root)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_GATE_FAILED
    assert report["ok"] is False
    assert report["links"]["broken_count"] == 1
    assert any(error["code"] == "broken-document-links" for error in report["errors"])
    assert report["publish"]["completed"] is False
    assert not (docs_root / contract.MANIFEST_FILENAME).exists()
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()
    assert _file_bytes(root) == before


def test_apply_uses_shared_project_root_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    events: list[tuple[str, Path, float | None]] = []

    @contextmanager
    def recording_lock(
        project_root: Path,
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 0.05,
    ):
        assert poll_interval == 0.05
        events.append(("enter", Path(project_root), timeout))
        try:
            yield object()
        finally:
            events.append(("exit", Path(project_root), timeout))

    monkeypatch.setattr(contract, "project_sync_lock", recording_lock)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        lock_timeout=2.0,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["publish"]["completed"] is True
    assert events == [
        ("enter", root.resolve(), 2.0),
        ("exit", root.resolve(), 2.0),
    ]


def test_unknown_manifest_schema_is_not_silently_rebuilt(tmp_path: Path) -> None:
    root, docs_root = _project(tmp_path)
    (docs_root / contract.MANIFEST_FILENAME).write_text(
        '{"schema_version": 99, "files": []}\n',
        encoding="utf-8",
    )
    before = _file_bytes(root)

    with pytest.raises(coordinator.SyncInputError, match="unknown schema"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    assert _file_bytes(root) == before


def test_unhandled_phase4_candidate_blocks_scan_baseline_advance(
    tmp_path: Path,
) -> None:
    root, docs_root = _project(tmp_path)
    (root / "module" / "__init__.py").unlink()
    (root / "module").rmdir()

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["phase4"]["counts"]["candidates"] == 1
    assert any(
        isinstance(warning, dict) and warning["code"] == "phase4-candidates-pending"
        for warning in report["warnings"]
    )
    assert any(
        isinstance(warning, dict) and warning["code"] == "scan-baseline-frozen"
        for warning in report["warnings"]
    )
    # Documents and the manifest publish normally; only the scan baseline stays
    # frozen until the candidate is quarantined.
    assert (docs_root / contract.MANIFEST_FILENAME).exists()
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_manifest_publish_failure_rolls_back_markers_and_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path, corrupt_marker=True)
    before = _file_bytes(root)
    original_set_state = coordinator._set_publish_path_state

    def fail_manifest(path: Path, before, after, **kwargs) -> None:
        if (
            path == docs_root / contract.MANIFEST_FILENAME
            and kwargs["label"] == "manifest"
            and kwargs["desired_after"] is True
            and kwargs["allow_both"] is False
        ):
            raise OSError("injected manifest failure")
        original_set_state(path, before, after, **kwargs)

    monkeypatch.setattr(coordinator, "_set_publish_path_state", fail_manifest)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["manifest_commit_point"] is False
    assert publish["rolled_back"] is True
    assert publish["rollback_failures"] == []
    assert not (docs_root / contract.MANIFEST_FILENAME).exists()
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()
    assert _file_bytes(root) == before


def test_scan_state_failure_reports_partial_commit_without_reverting_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    original_set_state = coordinator._set_publish_path_state

    def fail_scan_state(path: Path, before, after, **kwargs) -> None:
        if (
            path == docs_root / coordinator.SCAN_STATE_FILENAME
            and kwargs["label"] == "scan state"
            and kwargs["desired_after"] is True
            and kwargs["allow_both"] is False
        ):
            raise OSError("injected scan-state failure")
        original_set_state(path, before, after, **kwargs)

    monkeypatch.setattr(coordinator, "_set_publish_path_state", fail_scan_state)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["disposition"] == "partially-installed"
    assert publish["manifest_disposition"] == "installed"
    assert publish["scan_state_disposition"] == "not-installed"
    assert publish["manifest_commit_point"] is True
    assert publish["scan_state_committed"] is False
    assert publish["partial_commit"] is True
    assert "manifest=installed" in str(caught.value)
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_scan_state_only_failure_is_not_reported_as_partial_manifest_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    assert initial["publish"]["completed"] is True
    manifest_before = (docs_root / contract.MANIFEST_FILENAME).read_bytes()
    scan_before = (docs_root / coordinator.SCAN_STATE_FILENAME).read_bytes()
    _write(
        root / "module" / "__init__.py",
        "# -*- coding: utf-8 -*-\nVALUE = 1\n",
    )
    original_set_state = coordinator._set_publish_path_state

    def fail_scan_state(path: Path, before, after, **kwargs) -> None:
        if (
            path == docs_root / coordinator.SCAN_STATE_FILENAME
            and kwargs["label"] == "scan state"
            and kwargs["desired_after"] is True
            and kwargs["allow_both"] is False
        ):
            raise OSError("injected scan-state-only failure")
        original_set_state(path, before, after, **kwargs)

    monkeypatch.setattr(coordinator, "_set_publish_path_state", fail_scan_state)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp="2026-07-30T01:00:00+00:00",
        )

    result = caught.value.result
    publish = result["publish"]
    assert result["manifest"]["changed"] is False
    assert result["markers"]["candidate_count"] == 0
    assert result["claude"]["changed"] is False
    assert publish["disposition"] == "not-installed"
    assert publish["manifest_disposition"] == "unchanged"
    assert publish["scan_state_disposition"] == "not-installed"
    assert publish["manifest_commit_point"] is False
    assert publish["scan_state_committed"] is False
    assert publish["partial_commit"] is False
    assert publish["published"] == []
    assert "manifest=unchanged" in str(caught.value)
    assert "installed_artifacts=0" in str(caught.value)
    assert "manifest is committed" not in str(caught.value)
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == manifest_before
    assert (docs_root / coordinator.SCAN_STATE_FILENAME).read_bytes() == scan_before


def test_pending_phase4_journal_blocks_coordinator_before_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    assert initial["publish"]["completed"] is True
    manifest_before = (docs_root / contract.MANIFEST_FILENAME).read_bytes()
    scan_state_before = (docs_root / coordinator.SCAN_STATE_FILENAME).read_bytes()
    (root / "module" / "__init__.py").unlink()
    (root / "module").rmdir()

    class SimulatedCrash(BaseException):
        pass

    def crash_after_prepare(point: str) -> None:
        if point == "quarantine-journal-ready":
            raise SimulatedCrash(point)

    monkeypatch.setattr(phase4_exit, "_transaction_fault", crash_after_prepare)
    with pytest.raises(SimulatedCrash):
        phase4_exit.apply_quarantine(root)

    journals = [
        docs_root
        / phase4_exit.QUARANTINE_DIRNAME
        / phase4_exit._TRANSACTION_CANONICAL_FILENAME
    ]
    assert journals[0].is_file()
    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert exit_code == coordinator.EXIT_GATE_FAILED
    assert report["status"] == "phase4-recovery-required"
    assert report["scan"]["commit_needed"] is False
    assert report["scan"]["blocked_by_phase4_recovery"] is True
    assert report["planned_writes"] == []
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == manifest_before
    assert (
        docs_root / coordinator.SCAN_STATE_FILENAME
    ).read_bytes() == scan_state_before
    assert journals[0].is_file()


def test_pending_coordinator_journal_blocks_runs_and_recovers_clean_preimage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    before = _file_bytes(root)

    def crash_after_journal(point: str) -> None:
        if point == "publish-journal-ready":
            raise KeyboardInterrupt("journal-ready")

    monkeypatch.setattr(coordinator, "_publish_fault", crash_after_journal)
    with pytest.raises(KeyboardInterrupt, match="journal-ready"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    assert journal_path.is_file()
    assert _file_bytes(root) == before

    blocked, blocked_code = coordinator.synchronize_project(
        root,
        run_timestamp=SYNCED,
    )
    assert blocked_code == coordinator.EXIT_GATE_FAILED
    assert blocked["status"] == "coordinator-recovery-required"
    assert blocked["planned_writes"] == []

    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "rolled-back"
    assert not journal_path.exists()
    assert _file_bytes(root) == before


def test_precommit_process_loss_recovers_marker_and_claude_preimages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path, corrupt_marker=True)
    before = _file_bytes(root)

    def fail_before_manifest(_prepared: coordinator.PreparedSync) -> None:
        raise OSError("before-manifest")

    def terminate_during_rollback(
        _artifacts,
        _paths,
        _artifact_root,
        _artifact_root_identity,
    ) -> list[dict[str, str]]:
        raise KeyboardInterrupt("rollback-process-loss")

    monkeypatch.setattr(coordinator, "_documents_match_candidate", fail_before_manifest)
    monkeypatch.setattr(coordinator, "_rollback_artifacts", terminate_during_rollback)

    with pytest.raises(KeyboardInterrupt, match="rollback-process-loss"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    assert journal_path.is_file()
    assert (root / "CLAUDE.md").read_bytes() != before["CLAUDE.md"]
    assert (docs_root / "module.md").read_bytes() != before["project_docs/module.md"]
    assert not (docs_root / contract.MANIFEST_FILENAME).exists()

    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "rolled-back"
    assert not journal_path.exists()
    assert _file_bytes(root) == before


def test_forged_committed_slot_cannot_finalize_precommit_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)

    def interrupt_after_journal(point: str) -> None:
        if point == "publish-journal-ready":
            raise KeyboardInterrupt("journal-ready")

    monkeypatch.setattr(coordinator, "_publish_fault", interrupt_after_journal)
    with pytest.raises(KeyboardInterrupt, match="journal-ready"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    slots = contract.coordinator_publish_journal_paths(root)
    slots["prepared"].rename(slots["committed"])
    with pytest.raises(
        coordinator.SyncInputError,
        match="committed publish outcome does not match",
    ):
        coordinator.recover_project(root)
    assert slots["committed"].is_file()

    slots["committed"].rename(slots["prepared"])
    monkeypatch.setattr(coordinator, "_publish_fault", lambda _point: None)
    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "rolled-back"
    assert not any(path.exists() for path in slots.values())



def test_forged_rolled_back_slot_cannot_finalize_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path, corrupt_marker=True)
    before = _file_bytes(root)

    def fail_before_manifest(_prepared: coordinator.PreparedSync) -> None:
        raise OSError("before-manifest")

    def terminate_during_rollback(
        _artifacts,
        _paths,
        _artifact_root,
        _artifact_root_identity,
    ) -> list[dict[str, str]]:
        raise KeyboardInterrupt("rollback-process-loss")

    monkeypatch.setattr(coordinator, "_documents_match_candidate", fail_before_manifest)
    monkeypatch.setattr(coordinator, "_rollback_artifacts", terminate_during_rollback)
    with pytest.raises(KeyboardInterrupt, match="rollback-process-loss"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    slots = contract.coordinator_publish_journal_paths(root)
    slots["prepared"].rename(slots["rolled_back"])
    with pytest.raises(
        coordinator.SyncInputError,
        match="rolled_back publish outcome does not match",
    ):
        coordinator.recover_project(root)
    assert slots["rolled_back"].is_file()

    slots["rolled_back"].rename(slots["prepared"])
    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "rolled-back"
    assert _file_bytes(root) == before
    assert not any(path.exists() for path in slots.values())



def test_post_manifest_recovery_completes_scan_state_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)

    def interrupt_after_manifest(point: str) -> None:
        if point == "manifest-verified":
            raise KeyboardInterrupt("manifest-verified")

    monkeypatch.setattr(coordinator, "_publish_fault", interrupt_after_manifest)
    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    assert caught.value.result["publish"]["manifest_commit_point"] is True
    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    assert journal_path.is_file()
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()

    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "completed-forward"
    assert recovered["publish"]["completed"] is True
    assert not journal_path.exists()
    assert (
        scan_dirs.load_previous_state(docs_root / coordinator.SCAN_STATE_FILENAME)
        is not None
    )


def test_manifest_preimage_replacement_after_candidate_selection_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    assert initial["publish"]["completed"] is True

    external_manifest = b'{"external":"manifest replacement"}\n'
    original_select = coordinator._select_manifest

    def replace_after_selection(*args, **kwargs):
        selected = original_select(*args, **kwargs)
        (docs_root / contract.MANIFEST_FILENAME).write_bytes(external_manifest)
        return selected

    monkeypatch.setattr(coordinator, "_select_manifest", replace_after_selection)

    with pytest.raises(
        coordinator.SyncInputError, match="manifest changed during staging"
    ):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp="2026-07-30T01:00:00+00:00",
        )

    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == external_manifest


def test_scan_state_preimage_replacement_after_candidate_build_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    assert initial["publish"]["completed"] is True
    manifest_before = (docs_root / contract.MANIFEST_FILENAME).read_bytes()

    external_state = b'{"external":"scan-state replacement"}\n'
    original_scan_candidate = coordinator._scan_candidate

    def replace_after_candidate(*args, **kwargs):
        selected = original_scan_candidate(*args, **kwargs)
        (docs_root / coordinator.SCAN_STATE_FILENAME).write_bytes(external_state)
        return selected

    monkeypatch.setattr(coordinator, "_scan_candidate", replace_after_candidate)

    with pytest.raises(
        coordinator.SyncInputError, match="scan state changed during staging"
    ):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp="2026-07-30T01:00:00+00:00",
        )

    assert (docs_root / coordinator.SCAN_STATE_FILENAME).read_bytes() == external_state
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == manifest_before


def test_document_added_after_final_precommit_check_reports_partial_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    original_check = coordinator._documents_match_candidate

    def add_after_check(prepared: coordinator.PreparedSync) -> None:
        original_check(prepared)
        _write(docs_root / "late.md", "# Late\n")

    monkeypatch.setattr(coordinator, "_documents_match_candidate", add_after_check)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["manifest_commit_point"] is True
    assert publish["partial_commit"] is True
    assert publish["rolled_back"] is False
    assert (docs_root / "late.md").read_text(encoding="utf-8") == "# Late\n"
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()
    with pytest.raises(contract.ManifestValidationError):
        contract.load_manifest(docs_root, verify_files=True, verify_complete_set=True)


def test_document_modified_after_final_precommit_check_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    document = docs_root / "module.md"
    external_content = "# concurrent document replacement\n"
    original_check = coordinator._documents_match_candidate

    def replace_after_check(prepared: coordinator.PreparedSync) -> None:
        original_check(prepared)
        document.write_text(external_content, encoding="utf-8")

    monkeypatch.setattr(coordinator, "_documents_match_candidate", replace_after_check)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["manifest_commit_point"] is True
    assert publish["partial_commit"] is True
    assert document.read_text(encoding="utf-8") == external_content
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_noop_apply_rechecks_pending_phase4_journal_inside_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    assert initial["publish"]["completed"] is True
    manifest_before = (docs_root / contract.MANIFEST_FILENAME).read_bytes()
    scan_state_before = (docs_root / coordinator.SCAN_STATE_FILENAME).read_bytes()

    original_pending = phase4_exit.pending_recovery_status
    calls = 0
    journal = (
        docs_root
        / phase4_exit.QUARANTINE_DIRNAME
        / f"{phase4_exit._TRANSACTION_PREFIX}{'0' * 32}.json"
    )

    def create_journal_after_unlocked_gate(root_value):
        nonlocal calls
        calls += 1
        result = original_pending(root_value)
        if calls == 2:
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text("{", encoding="utf-8")
        return result

    monkeypatch.setattr(
        phase4_exit,
        "pending_recovery_status",
        create_journal_after_unlocked_gate,
    )

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert calls == 3
    assert exit_code == coordinator.EXIT_GATE_FAILED
    assert report["status"] == "phase4-recovery-required"
    assert report["planned_writes"] == []
    assert report["publish"]["completed"] is False
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == manifest_before
    assert (
        docs_root / coordinator.SCAN_STATE_FILENAME
    ).read_bytes() == scan_state_before
    assert journal.read_text(encoding="utf-8") == "{"


def test_marker_post_replace_keyboard_interrupt_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path, corrupt_marker=True)
    before = _file_bytes(root)
    original_set_snapshot = phase4_exit._set_snapshot_state
    interrupted = False

    def interrupt_after_marker_replace(
        path: Path,
        base: Path,
        description: str,
        desired,
        allowed_current,
        **kwargs,
    ) -> None:
        nonlocal interrupted
        original_set_snapshot(
            path,
            base,
            description,
            desired,
            allowed_current,
            **kwargs,
        )
        if (
            path == docs_root / "module.md"
            and description == "marker"
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt("marker-post-replace")

    monkeypatch.setattr(
        phase4_exit,
        "_set_snapshot_state",
        interrupt_after_marker_replace,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert interrupted is True
    assert publish["manifest_commit_point"] is False
    assert publish["rolled_back"] is True
    assert publish["rollback_failures"] == []
    assert _file_bytes(root) == before


def test_claude_post_replace_keyboard_interrupt_is_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    before = _file_bytes(root)
    original_set_snapshot = phase4_exit._set_snapshot_state
    interrupted = False

    def interrupt_after_claude_replace(
        path: Path,
        base: Path,
        description: str,
        desired,
        allowed_current,
        **kwargs,
    ) -> None:
        nonlocal interrupted
        original_set_snapshot(
            path,
            base,
            description,
            desired,
            allowed_current,
            **kwargs,
        )
        if (
            path == root / "CLAUDE.md"
            and description == "claude-index"
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt("claude-post-replace")

    monkeypatch.setattr(
        phase4_exit,
        "_set_snapshot_state",
        interrupt_after_claude_replace,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert interrupted is True
    assert publish["manifest_commit_point"] is False
    assert publish["rolled_back"] is True
    assert publish["rollback_failures"] == []
    assert _file_bytes(root) == before


def test_manifest_post_replace_keyboard_interrupt_is_partial_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    original_set_snapshot = phase4_exit._set_snapshot_state
    interrupted = False

    def interrupt_after_manifest_replace(
        path: Path,
        base: Path,
        description: str,
        desired,
        allowed_current,
        **kwargs,
    ) -> None:
        nonlocal interrupted
        original_set_snapshot(
            path,
            base,
            description,
            desired,
            allowed_current,
            **kwargs,
        )
        if (
            path == docs_root / contract.MANIFEST_FILENAME
            and description == "manifest"
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt("manifest-post-replace")

    monkeypatch.setattr(
        phase4_exit,
        "_set_snapshot_state",
        interrupt_after_manifest_replace,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert interrupted is True
    assert publish["manifest_commit_point"] is True
    assert publish["partial_commit"] is True
    assert publish["rolled_back"] is False
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_scan_state_post_replace_keyboard_interrupt_is_reported_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    original_set_snapshot = phase4_exit._set_snapshot_state
    interrupted = False

    def interrupt_after_scan_replace(
        path: Path,
        base: Path,
        description: str,
        desired,
        allowed_current,
        **kwargs,
    ) -> None:
        nonlocal interrupted
        original_set_snapshot(
            path,
            base,
            description,
            desired,
            allowed_current,
            **kwargs,
        )
        if (
            path == docs_root / coordinator.SCAN_STATE_FILENAME
            and description == "scan state"
            and not interrupted
        ):
            interrupted = True
            raise KeyboardInterrupt("scan-post-replace")

    monkeypatch.setattr(
        phase4_exit,
        "_set_snapshot_state",
        interrupt_after_scan_replace,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert interrupted is True
    assert publish["manifest_commit_point"] is True
    assert publish["scan_state_committed"] is True
    assert publish["partial_commit"] is True
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2
    committed_state = scan_dirs.load_previous_state(
        docs_root / coordinator.SCAN_STATE_FILENAME
    )
    assert committed_state is not None
    assert committed_state["state_role"] == "committed"


def test_manifest_post_replace_with_failed_readback_is_never_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    original_set_snapshot = phase4_exit._set_snapshot_state
    original_optional_bytes = coordinator._optional_bytes
    manifest_replaced = False

    def fail_after_manifest_replace(
        path: Path,
        base: Path,
        description: str,
        desired,
        allowed_current,
        **kwargs,
    ) -> None:
        nonlocal manifest_replaced
        original_set_snapshot(
            path,
            base,
            description,
            desired,
            allowed_current,
            **kwargs,
        )
        if (
            path == docs_root / contract.MANIFEST_FILENAME
            and description == "manifest"
        ):
            manifest_replaced = True
            raise OSError("injected post-replace failure")

    def fail_manifest_readback(path: Path) -> bytes | None:
        if manifest_replaced and path == docs_root / contract.MANIFEST_FILENAME:
            raise OSError("injected manifest readback failure")
        return original_optional_bytes(path)

    monkeypatch.setattr(
        phase4_exit,
        "_set_snapshot_state",
        fail_after_manifest_replace,
    )
    monkeypatch.setattr(coordinator, "_optional_bytes", fail_manifest_readback)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert manifest_replaced is True
    assert publish["manifest_commit_point"] is False
    assert publish["manifest_commit_ambiguous"] is True
    assert publish["partial_commit"] is True
    assert publish["rolled_back"] is False
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_marker_source_changed_between_collection_and_staging_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path, corrupt_marker=True)
    document = docs_root / "module.md"
    concurrent_content = document.read_text(encoding="utf-8") + "\n并发新增内容。\n"
    original_collect = coordinator._normalized_marker_files

    def mutate_after_collection(project_docs: Path):
        result = original_collect(project_docs)
        document.write_text(concurrent_content, encoding="utf-8")
        return result

    monkeypatch.setattr(
        coordinator,
        "_normalized_marker_files",
        mutate_after_collection,
    )

    with pytest.raises(
        coordinator.SyncInputError,
        match="marker source changed between collection and staging",
    ):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    assert document.read_text(encoding="utf-8") == concurrent_content
    assert not (docs_root / contract.MANIFEST_FILENAME).exists()
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_claude_candidate_never_rereads_live_path_after_preimage_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    claude_path = root / "CLAUDE.md"
    original = claude_path.read_text(encoding="utf-8")
    transient = original.replace("manual-before", "transient-manual-content")
    reread_calls = 0

    def transient_claude_reread(path: Path) -> str:
        nonlocal reread_calls
        reread_calls += 1
        claude_path.write_text(transient, encoding="utf-8")
        try:
            return transient
        finally:
            claude_path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(claude_doc_index, "read_utf8", transient_claude_reread)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["publish"]["completed"] is True
    assert reread_calls == 0
    committed = claude_path.read_text(encoding="utf-8")
    assert "manual-before" in committed
    assert "transient-manual-content" not in committed


def test_manifest_candidate_is_derived_from_captured_bytes_during_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    original_bytes = manifest_path.read_bytes()
    transient_manifest = json.loads(original_bytes.decode("utf-8"))
    transient_manifest["last_synced"] = "2099-01-01T00:00:00+00:00"
    transient_bytes = contract.stable_json_dumps(transient_manifest).encode("utf-8")
    original_loader = coordinator._load_current_manifest

    def load_during_aba(paths, captured_bytes, captured_docs):
        manifest_path.write_bytes(transient_bytes)
        try:
            return original_loader(paths, captured_bytes, captured_docs)
        finally:
            manifest_path.write_bytes(original_bytes)

    monkeypatch.setattr(coordinator, "_load_current_manifest", load_during_aba)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["manifest"]["changed"] is False
    assert report["publish"]["published"] == []
    assert manifest_path.read_bytes() == original_bytes


def test_captured_manifest_is_validated_against_frozen_document_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    document = docs_root / "module.md"
    original_document = document.read_bytes()
    original_loader = coordinator._load_current_manifest

    def load_while_live_document_is_transient(paths, captured_bytes, captured_docs):
        document.write_bytes(b"# transient live document\n")
        try:
            return original_loader(paths, captured_bytes, captured_docs)
        finally:
            document.write_bytes(original_document)

    monkeypatch.setattr(
        coordinator,
        "_load_current_manifest",
        load_while_live_document_is_transient,
    )

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["manifest"]["changed"] is False
    assert report["planned_writes"] == []
    assert document.read_bytes() == original_document


def test_validation_uses_the_stable_source_snapshot_during_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    transient_file = root / "module" / "payload.py"
    original_validate = coordinator._validate_documents

    def validate_during_source_aba(*args, **kwargs):
        transient_file.write_text("value = 1\n", encoding="utf-8")
        try:
            return original_validate(*args, **kwargs)
        finally:
            transient_file.unlink()

    monkeypatch.setattr(coordinator, "_validate_documents", validate_during_source_aba)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["validation"]["blocking_failure_count"] == 0
    assert report["publish"]["completed"] is True
    assert not transient_file.exists()


def test_phase4_uses_frozen_absence_during_source_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    ghost_doc = _placeholder_document().replace("module", "ghost")
    _write(docs_root / "ghost.md", ghost_doc)
    transient_source = root / "ghost"
    original_phase4 = coordinator._phase4_report

    def phase4_during_source_aba(*args, **kwargs):
        transient_source.mkdir()
        try:
            return original_phase4(*args, **kwargs)
        finally:
            transient_source.rmdir()

    monkeypatch.setattr(coordinator, "_phase4_report", phase4_during_source_aba)

    report, exit_code = coordinator.synchronize_project(
        root,
        run_timestamp=SYNCED,
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["phase4"]["counts"]["candidates"] == 1
    assert report["phase4"]["candidates"][0]["path"] == "ghost.md"
    assert not transient_source.exists()


def test_scan_candidate_is_derived_from_captured_bytes_during_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    state_path = docs_root / coordinator.SCAN_STATE_FILENAME
    original_bytes = state_path.read_bytes()
    transient_state = json.loads(original_bytes.decode("utf-8"))
    transient_state["committed_at"] = "2099-01-01T00:00:00+00:00"
    transient_bytes = contract.stable_json_dumps(transient_state).encode("utf-8")
    original_scan_candidate = coordinator._scan_candidate
    observed_precondition: dict[str, object] = {}

    def scan_during_aba(
        paths,
        captured_bytes,
        *,
        force_full,
        manifest_generation_id,
    ):
        state_path.write_bytes(transient_bytes)
        try:
            selected = original_scan_candidate(
                paths,
                captured_bytes,
                force_full=force_full,
                manifest_generation_id=manifest_generation_id,
            )
            observed_precondition.update(selected[1]["baseline_precondition"])
            return selected
        finally:
            state_path.write_bytes(original_bytes)

    monkeypatch.setattr(coordinator, "_scan_candidate", scan_during_aba)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["planned_writes"] == []
    assert observed_precondition["sha256"] == contract.sha256_bytes(original_bytes)
    assert observed_precondition["sha256"] != contract.sha256_bytes(transient_bytes)
    assert state_path.read_bytes() == original_bytes


def test_successful_noop_apply_still_verifies_under_lock(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    committed = _file_bytes(root)

    report, exit_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp="2026-07-30T01:00:00+00:00",
    )

    assert exit_code == coordinator.EXIT_OK
    assert report["planned_writes"] == []
    assert report["publish"]["completed"] is True
    assert report["publish"]["disposition"] == "unchanged"
    assert report["publish"]["manifest_disposition"] == "unchanged"
    assert report["publish"]["scan_state_disposition"] == "unchanged"
    assert report["publish"]["manifest_commit_point"] is False
    assert report["publish"]["partial_commit"] is False
    assert report["publish"]["published"] == []
    assert _file_bytes(root) == committed


def test_document_deleted_after_final_precommit_check_reports_partial_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    document = docs_root / "module.md"
    original_check = coordinator._documents_match_candidate

    def delete_after_check(prepared: coordinator.PreparedSync) -> None:
        original_check(prepared)
        document.unlink()

    monkeypatch.setattr(coordinator, "_documents_match_candidate", delete_after_check)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["manifest_commit_point"] is True
    assert publish["partial_commit"] is True
    assert not document.exists()
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_document_change_after_manifest_verification_blocks_scan_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    document = docs_root / "module.md"
    external_content = "# changed after manifest verification\n"
    original_verify = coordinator._verify_committed_manifest
    calls = 0

    def change_after_first_verification(prepared: coordinator.PreparedSync) -> None:
        nonlocal calls
        original_verify(prepared)
        calls += 1
        if calls == 1:
            document.write_text(external_content, encoding="utf-8")

    monkeypatch.setattr(
        coordinator,
        "_verify_committed_manifest",
        change_after_first_verification,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert calls == 1
    assert publish["manifest_commit_point"] is True
    assert publish["partial_commit"] is True
    assert document.read_text(encoding="utf-8") == external_content
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_scan_state_build_keyboard_interrupt_happens_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)

    def interrupt_build(candidate, state_path):
        raise KeyboardInterrupt("scan-build")

    monkeypatch.setattr(
        coordinator,
        "_build_committed_scan_state",
        interrupt_build,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["manifest_commit_point"] is False
    assert publish["scan_state_committed"] is False
    assert publish["scan_state_commit_ambiguous"] is False
    assert publish["partial_commit"] is False
    assert publish["rolled_back"] is True
    assert not (docs_root / contract.MANIFEST_FILENAME).exists()
    assert not (docs_root / coordinator.SCAN_STATE_FILENAME).exists()


def test_scan_state_foreign_bytes_after_failure_are_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    foreign_state = b'{"foreign":"scan state"}\n'
    state_path = docs_root / coordinator.SCAN_STATE_FILENAME
    original_rename = phase4_exit._rename_no_replace

    def install_foreign_scan_state(source: Path, destination: Path) -> None:
        if Path(destination) == state_path:
            state_path.write_bytes(foreign_state)
            raise FileExistsError("injected foreign scan state")
        original_rename(source, destination)

    monkeypatch.setattr(
        phase4_exit,
        "_rename_no_replace",
        install_foreign_scan_state,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["disposition"] == "unknown"
    assert publish["manifest_commit_point"] is True
    assert publish["scan_state_committed"] is False
    assert publish["scan_state_commit_ambiguous"] is True
    assert publish["partial_commit"] is True
    assert (docs_root / coordinator.SCAN_STATE_FILENAME).read_bytes() == foreign_state


def test_manifest_foreign_bytes_after_failure_are_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    foreign_manifest = b'{"foreign":"manifest"}\n'
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    original_rename = phase4_exit._rename_no_replace

    def install_foreign_manifest(source: Path, destination: Path) -> None:
        if Path(destination) == manifest_path:
            manifest_path.write_bytes(foreign_manifest)
            raise FileExistsError("injected foreign manifest")
        original_rename(source, destination)

    monkeypatch.setattr(
        phase4_exit,
        "_rename_no_replace",
        install_foreign_manifest,
    )

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["manifest_commit_point"] is False
    assert publish["manifest_commit_ambiguous"] is True
    assert publish["partial_commit"] is True
    assert publish["rolled_back"] is False
    assert (docs_root / contract.MANIFEST_FILENAME).read_bytes() == foreign_manifest


def test_lock_release_failure_after_commit_reports_installed_disposition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)

    @contextmanager
    def fail_on_release(
        project_root: Path,
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 0.05,
    ):
        assert project_root == root.resolve()
        assert timeout == 10.0
        assert poll_interval == 0.05
        yield object()
        raise OSError("injected lock release failure")

    monkeypatch.setattr(contract, "project_sync_lock", fail_on_release)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["completed"] is False
    assert publish["disposition"] == "installed"
    assert publish["manifest_disposition"] == "installed"
    assert publish["scan_state_disposition"] == "installed"
    assert publish["manifest_commit_point"] is True
    assert publish["scan_state_committed"] is True
    assert publish["partial_commit"] is False
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2
    assert (
        scan_dirs.load_previous_state(docs_root / coordinator.SCAN_STATE_FILENAME)
        is not None
    )


def test_noop_lock_release_failure_is_unchanged_not_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    initial, initial_code = coordinator.synchronize_project(
        root,
        apply=True,
        run_timestamp=SYNCED,
    )
    assert initial_code == coordinator.EXIT_OK
    assert initial["publish"]["completed"] is True
    before = _file_bytes(root)

    @contextmanager
    def fail_on_release(
        project_root: Path,
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 0.05,
    ):
        assert project_root == root.resolve()
        assert timeout == 10.0
        assert poll_interval == 0.05
        yield object()
        raise OSError("injected no-op lock release failure")

    monkeypatch.setattr(contract, "project_sync_lock", fail_on_release)

    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp="2026-07-30T01:00:00+00:00",
        )

    publish = caught.value.result["publish"]
    assert publish["completed"] is False
    assert publish["disposition"] == "unchanged"
    assert publish["manifest_disposition"] == "unchanged"
    assert publish["scan_state_disposition"] == "unchanged"
    assert publish["manifest_commit_point"] is False
    assert publish["manifest_commit_ambiguous"] is False
    assert publish["scan_state_committed"] is False
    assert publish["scan_state_commit_ambiguous"] is False
    assert publish["partial_commit"] is False
    assert publish["published"] == []
    assert _file_bytes(root) == before


def test_partial_canonical_journal_publish_blocks_until_manual_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)

    original_rename = phase4_exit._rename_no_replace

    def publish_partial(source: Path, target: Path) -> None:
        if Path(target) == journal_path:
            payload = Path(source).read_bytes()
            journal_path.write_bytes(payload[: max(1, len(payload) // 2)])
            return
        original_rename(source, target)

    monkeypatch.setattr(phase4_exit, "_rename_no_replace", publish_partial)
    try:
        with pytest.raises(coordinator.SyncInputError, match="exact readback"):
            coordinator._publish_journal_exclusive(
                journal_path,
                {"transaction": "candidate"},
            )
        assert journal_path.is_file()
        assert journal_path.read_bytes()
        with pytest.raises(contract.JsonContractError):
            coordinator._load_publish_journal(paths)
    finally:
        journal_path.unlink(missing_ok=True)
        coordinator._cleanup_publish_journal_temps(journal_path)


def test_journal_publish_is_no_replace_and_preserves_foreign_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)
    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    foreign = b"foreign canonical journal\n"

    original_rename = phase4_exit._rename_no_replace

    def lose_publish_race(_source: Path, target: Path) -> None:
        if Path(target) == journal_path:
            journal_path.write_bytes(foreign)
            raise FileExistsError("injected competing journal")
        original_rename(_source, target)

    monkeypatch.setattr(phase4_exit, "_rename_no_replace", lose_publish_race)
    try:
        with pytest.raises(coordinator.SyncInputError, match="concurrent"):
            coordinator._publish_journal_exclusive(
                journal_path,
                {"transaction": "candidate"},
            )
        assert journal_path.read_bytes() == foreign
    finally:
        journal_path.unlink(missing_ok=True)
        coordinator._cleanup_publish_journal_temps(journal_path)


def test_stale_partial_journal_temp_blocks_publication_without_cleanup(
    tmp_path: Path,
) -> None:
    root, _ = _project(tmp_path)
    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    residue = journal_path.with_name(
        f"{journal_path.name}{coordinator._PUBLISH_JOURNAL_TEMP_MARKER}dead.tmp"
    )
    residue.write_bytes(b"partial journal")

    try:
        report, exit_code = coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

        assert exit_code == coordinator.EXIT_GATE_FAILED
        assert report["status"] == "coordinator-recovery-required"
        assert report["planned_writes"] == []
        assert report["publish"]["completed"] is False
        assert residue.read_bytes() == b"partial journal"
        assert not journal_path.exists()
    finally:
        residue.unlink(missing_ok=True)


def test_corrupt_canonical_journal_blocks_runs_and_recovery(
    tmp_path: Path,
) -> None:
    root, _ = _project(tmp_path)
    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    journal_path.write_bytes(b'{"schema_version":')
    try:
        blocked, blocked_code = coordinator.synchronize_project(
            root,
            run_timestamp=SYNCED,
        )
        assert blocked_code == coordinator.EXIT_GATE_FAILED
        assert blocked["status"] == "coordinator-recovery-required"
        assert "detail" in blocked["recovery"]
        with pytest.raises(contract.JsonContractError):
            coordinator.recover_project(root)
        assert journal_path.is_file()
    finally:
        journal_path.unlink(missing_ok=True)


def test_recovery_rejects_foreign_artifact_and_retains_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path, corrupt_marker=True)
    document = docs_root / "module.md"
    original = document.read_bytes()

    def interrupt_after_journal(point: str) -> None:
        if point == "publish-journal-ready":
            raise KeyboardInterrupt("journal-ready")

    monkeypatch.setattr(coordinator, "_publish_fault", interrupt_after_journal)
    with pytest.raises(KeyboardInterrupt, match="journal-ready"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    document.write_bytes(b"foreign document content\n")
    with pytest.raises(
        coordinator.SyncInputError,
        match=r"CAS refused \[recovery-conflict\]",
    ):
        coordinator.recover_project(root)
    assert journal_path.is_file()

    document.write_bytes(original)
    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "rolled-back"
    assert not journal_path.exists()


def test_recovery_rejects_foreign_manifest_and_retains_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)

    def interrupt_after_journal(point: str) -> None:
        if point == "publish-journal-ready":
            raise KeyboardInterrupt("journal-ready")

    monkeypatch.setattr(coordinator, "_publish_fault", interrupt_after_journal)
    with pytest.raises(KeyboardInterrupt, match="journal-ready"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    manifest_path = docs_root / contract.MANIFEST_FILENAME
    manifest_path.write_bytes(b'{"foreign":"manifest"}\n')
    with pytest.raises(
        coordinator.SyncInputError, match="neither the recorded preimage"
    ):
        coordinator.recover_project(root)
    assert journal_path.is_file()
    assert manifest_path.read_bytes() == b'{"foreign":"manifest"}\n'

    manifest_path.unlink()
    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "rolled-back"
    assert not journal_path.exists()


def test_committed_journal_finalization_failure_requires_cleanup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, docs_root = _project(tmp_path)
    original_fault = coordinator._publish_fault

    def fail_before_history(point: str) -> None:
        if point == "journal-finalize-before-history":
            raise OSError("injected journal history failure")
        original_fault(point)

    monkeypatch.setattr(coordinator, "_publish_fault", fail_before_history)
    with pytest.raises(coordinator.SyncPublishError) as caught:
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    publish = caught.value.result["publish"]
    assert publish["disposition"] == "installed"
    assert publish["manifest_disposition"] == "installed"
    assert publish["scan_state_disposition"] == "installed"
    assert publish["partial_commit"] is False
    assert publish["finalization_pending"] is True
    slots = contract.coordinator_publish_journal_paths(root)
    assert not slots["prepared"].exists()
    assert slots["committed"].is_file()
    assert not slots["rolled_back"].exists()
    assert contract.load_manifest(docs_root, verify_files=True)["schema_version"] == 2

    monkeypatch.setattr(coordinator, "_publish_fault", original_fault)
    recovered, recovered_code = coordinator.recover_project(root)
    assert recovered_code == coordinator.EXIT_OK
    assert recovered["recovery"]["action"] == "finalized-committed"
    assert recovered["publish"]["disposition"] == "installed"
    assert not any(path.exists() for path in slots.values())


def test_recovery_rejects_hardlinked_journal_without_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)

    def interrupt_after_journal(point: str) -> None:
        if point == "publish-journal-ready":
            raise KeyboardInterrupt("journal-ready")

    monkeypatch.setattr(coordinator, "_publish_fault", interrupt_after_journal)
    with pytest.raises(KeyboardInterrupt, match="journal-ready"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    paths = coordinator._project_paths(root)
    journal_path = coordinator._publish_journal_path(paths)
    loaded = coordinator._load_publish_journal(paths)
    assert loaded is not None
    artifact_root = coordinator._publish_artifact_root(paths, loaded.journal)
    residue = journal_path.with_name(
        f"{journal_path.name}{coordinator._PUBLISH_JOURNAL_TEMP_MARKER}linked.tmp"
    )
    os.link(journal_path, residue)
    assert os.path.samefile(journal_path, residue)

    try:
        with pytest.raises(
            coordinator.SyncInputError,
            match="must have exactly one filesystem link",
        ):
            coordinator.recover_project(root)
        assert journal_path.is_file()
        assert residue.is_file()
        assert os.path.samefile(journal_path, residue)
    finally:
        residue.unlink(missing_ok=True)
        journal_path.unlink(missing_ok=True)
        if artifact_root.exists():
            artifact_root.rmdir()


def test_cli_recover_resolves_pending_coordinator_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _ = _project(tmp_path)

    def interrupt_after_journal(point: str) -> None:
        if point == "publish-journal-ready":
            raise KeyboardInterrupt("journal-ready")

    monkeypatch.setattr(coordinator, "_publish_fault", interrupt_after_journal)
    with pytest.raises(KeyboardInterrupt, match="journal-ready"):
        coordinator.synchronize_project(
            root,
            apply=True,
            run_timestamp=SYNCED,
        )

    completed = subprocess.run(
        [
            sys.executable,
            str(SYNC_TOOL),
            str(root),
            "--recover",
            "--json",
        ],
        cwd=root,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == coordinator.EXIT_OK
    assert completed.stderr == ""
    result = json.loads(completed.stdout)
    assert result["mode"] == "recover"
    assert result["recovery"]["action"] == "rolled-back"
    assert result["publish"]["disposition"] == "not-installed"
    paths = coordinator._project_paths(root)
    assert not coordinator._publish_journal_path(paths).exists()
