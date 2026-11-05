# -*- coding: utf-8 -*-
"""Focused contract tests for the schema-v2 directory scanner."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import project_docs_contract as contract
import scan_dirs


def _mkdir_with_file(path: Path, name: str = "data.txt", text: str = "data") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / name
    file_path.write_text(text, encoding="utf-8")
    return file_path


def _snapshot_map(state: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in state["snapshot"]["directories"]}


def _tamper_candidate(candidate: dict, field: str) -> dict:
    tampered = copy.deepcopy(candidate)
    if field == "directories":
        tampered["directories"] = []
    elif field == "deleted_directories":
        tampered["deleted_directories"] = [{"path": "phantom", "change": "deleted"}]
    elif field == "unknown_directories":
        tampered["unknown_directories"] = [{"path": "phantom", "change": "unknown"}]
    elif field == "changes":
        tampered["changes"]["new"] = []
    elif field == "change_counts":
        tampered["change_counts"]["new"] = 0
    elif field in (
        "total",
        "full_total",
        "max_depth",
        "boundary_count",
        "skipped_directory_count",
    ):
        tampered[field] = int(tampered[field]) + 1
    elif field == "mode":
        tampered["mode"] = "incremental"
    else:  # pragma: no cover - protects the test helper itself
        raise AssertionError(field)
    return tampered


def test_source_starts_with_utf8_cookie() -> None:
    source = Path(scan_dirs.__file__).read_text(encoding="utf-8")
    assert source.splitlines()[0] == "# -*- coding: utf-8 -*-"


def test_v2_snapshot_fingerprint_covers_direct_metadata_and_structure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "CLAUDE.md").write_text("root is not a boundary", encoding="utf-8")
    package = root / "package"
    direct_file = _mkdir_with_file(package, "module.py", "print('ok')\n")
    (package / "child").mkdir()
    fixed_mtime_ns = 1_700_000_000_123_456_700
    os.utime(direct_file, ns=(fixed_mtime_ns, fixed_mtime_ns))

    candidate = scan_dirs.build_candidate_state(root)
    records = _snapshot_map(candidate)

    assert candidate["schema_version"] == 2
    assert candidate["state_role"] == "candidate"
    assert {"package", "package/child"} <= records.keys()
    package_record = records["package"]
    assert package_record["snapshot"]["files"] == [
        {
            "name": "module.py",
            "path": "module.py",
            "file_type": "regular",
            "size": direct_file.stat().st_size,
            "mtime_ns": direct_file.stat().st_mtime_ns,
            "sha256": hashlib.sha256(direct_file.read_bytes()).hexdigest(),
        }
    ]
    assert package_record["snapshot"]["child_directories"] == ["child"]
    assert package_record["snapshot"]["is_boundary"] is False
    assert len(package_record["fingerprint"]) == 64

    old_fingerprint = package_record["fingerprint"]
    (package / "second_child").mkdir()
    rescanned = scan_dirs.build_candidate_state(root, candidate, incremental=True)
    assert _snapshot_map(rescanned)["package"]["fingerprint"] != old_fingerprint
    assert "package" in rescanned["changes"]["modified"]
    assert "package/second_child" in rescanned["changes"]["new"]


def test_same_size_and_mtime_content_change_is_modified(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    direct_file = _mkdir_with_file(root / "package", "payload.bin", "AAAA")
    baseline = scan_dirs.build_candidate_state(root)
    original_stat = direct_file.stat()
    original_sha = _snapshot_map(baseline)["package"]["snapshot"]["files"][0]["sha256"]

    direct_file.write_text("BBBB", encoding="utf-8")
    os.utime(
        direct_file,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    candidate = scan_dirs.build_candidate_state(root, baseline, incremental=True)

    assert direct_file.stat().st_size == original_stat.st_size
    assert direct_file.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert candidate["changes"]["modified"] == ["package"]
    assert (
        _snapshot_map(candidate)["package"]["snapshot"]["files"][0]["sha256"]
        != original_sha
    )


def test_direct_file_symlink_is_rejected_without_following_outside_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    package = root / "package"
    package.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = package / "linked.txt"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlink creation is unavailable: {exc}")

    with pytest.raises(scan_dirs.ScanError, match="symlink or reparse"):
        scan_dirs.build_candidate_state(root)


def test_exclusions_use_unicode_casefold(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "NoDe_MoDuLeS" / "dependency")
    _mkdir_with_file(root / "PrOjEcT_DoCs" / "generated")
    _mkdir_with_file(root / "visible")

    paths = set(_snapshot_map(scan_dirs.build_candidate_state(root)))

    assert paths == {"", "visible"}


def test_unicode_casefold_collision_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    package = root / "package"
    package.mkdir(parents=True)
    (package / "Straße.txt").write_text("one", encoding="utf-8")
    (package / "STRASSE.txt").write_text("two", encoding="utf-8")
    if len(list(package.iterdir())) != 2:
        pytest.skip("filesystem does not permit the Unicode collision fixture")

    with pytest.raises(scan_dirs.ScanError, match="casefold-colliding"):
        scan_dirs.build_candidate_state(root)


def test_incremental_classifies_new_modified_deleted_renamed_and_unchanged(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "stable")
    changed_file = _mkdir_with_file(root / "changed")
    _mkdir_with_file(root / "gone")
    _mkdir_with_file(root / "container" / "old")

    baseline = scan_dirs.build_candidate_state(root)
    changed_file.write_text("different length", encoding="utf-8")
    (root / "gone" / "data.txt").unlink()
    (root / "gone").rmdir()
    (root / "container" / "old").rename(root / "container" / "new")
    _mkdir_with_file(root / "added")

    candidate = scan_dirs.build_candidate_state(root, baseline, incremental=True)

    assert candidate["mode"] == "incremental"
    assert set(candidate["changes"]["new"]) == {"added", "container/new"}
    assert {"changed", "container"} <= set(candidate["changes"]["modified"])
    assert set(candidate["changes"]["deleted"]) == {"container/old", "gone"}
    assert "stable" in candidate["changes"]["unchanged"]
    assert candidate["changes"]["unknown"] == []
    assert {entry["path"] for entry in candidate["directories"]} == {
        "",
        "added",
        "changed",
        "container",
        "container/new",
    }
    assert {entry["path"] for entry in candidate["deleted_directories"]} == {
        "container/old",
        "gone",
    }


def test_nested_claude_boundary_stops_recursion_and_descendants_become_unknown(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "CLAUDE.md").write_text("root boundary must be ignored", encoding="utf-8")
    _mkdir_with_file(root / "open" / "deep")
    _mkdir_with_file(root / "bounded" / "deep")

    baseline = scan_dirs.build_candidate_state(root)
    baseline_paths = _snapshot_map(baseline)
    assert "open/deep" in baseline_paths
    assert "bounded/deep" in baseline_paths

    (root / "bounded" / "cLaUdE.Md").write_text(
        "nested boundary",
        encoding="utf-8",
    )
    candidate = scan_dirs.build_candidate_state(root, baseline, incremental=True)
    records = _snapshot_map(candidate)

    assert "open/deep" in records
    assert records["bounded"]["is_boundary"] is True
    assert "bounded/deep" not in records
    assert "bounded/deep" in candidate["changes"]["unknown"]
    assert "bounded/deep" not in candidate["changes"]["deleted"]
    assert {
        "path": "bounded",
        "reason": "boundary",
        "scope": "descendants",
    } in candidate["snapshot"]["unknown_scopes"]


def test_outside_root_directory_symlink_is_skipped_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "inside")
    outside = tmp_path / "outside"
    _mkdir_with_file(outside / "secret")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    candidate = scan_dirs.build_candidate_state(root)
    paths = _snapshot_map(candidate)

    assert "inside" in paths
    assert "escape" not in paths
    assert "escape/secret" not in paths
    assert candidate["skipped_directory_count"] == 1
    skipped = candidate["snapshot"]["skipped_directories"][0]
    assert skipped["path"] == "escape"
    assert skipped["reason"] == "outside-root"
    assert skipped["kind"] == "symlink"
    assert skipped["target"] == "<outside-root>"
    assert {
        "path": "escape",
        "reason": "outside-root",
        "scope": "subtree",
    } in candidate["snapshot"]["unknown_scopes"]


def test_candidate_does_not_replace_state_commit_is_atomic_and_zero_change_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)

    replace_calls: list[tuple[Path, Path]] = []
    real_replace = contract.os.replace

    def recording_replace(source: str | Path, target: str | Path) -> None:
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(contract.os, "replace", recording_replace)
    first = scan_dirs.build_candidate_state(
        root,
        baseline_precondition=absent,
    )
    scan_dirs.write_candidate_state(candidate_path, first)

    state_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    with pytest.raises(scan_dirs.BaselineConflict, match="baseline changed"):
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=first["candidate_generation_id"],
        )
    assert contract.read_json_strict(state_path) == {"sentinel": True}
    assert contract.read_json_strict(candidate_path)["state_role"] == "candidate"

    state_path.unlink()
    first = scan_dirs.build_candidate_state(
        root,
        baseline_precondition=absent,
    )
    scan_dirs.write_candidate_state(candidate_path, first)
    committed = scan_dirs.commit_candidate_state(
        candidate_path,
        state_path,
        project_root=root,
        expected_baseline=absent,
        expected_candidate_generation_id=first["candidate_generation_id"],
    )
    assert committed["state_role"] == "committed"
    assert contract.read_json_strict(state_path)["state_role"] == "committed"
    assert contract.read_json_strict(candidate_path)["state_role"] == "candidate"
    assert all(source.parent == target.parent for source, target in replace_calls)

    baseline_bytes = state_path.read_bytes()
    previous = scan_dirs.load_previous_state(state_path)
    zero_change = scan_dirs.build_candidate_state(root, previous, incremental=True)
    assert zero_change["directories"] == []
    assert zero_change["total"] == 0
    assert zero_change["change_counts"] == {
        "new": 0,
        "modified": 0,
        "deleted": 0,
        "unchanged": 2,
        "unknown": 0,
    }
    assert zero_change["snapshot"]["directories"]

    monkeypatch.chdir(tmp_path)
    scan_dirs.write_candidate_state("bare-candidate.json", zero_change)
    assert (tmp_path / "bare-candidate.json").is_file()
    assert state_path.read_bytes() == baseline_bytes


@pytest.mark.parametrize(
    "field",
    [
        "directories",
        "deleted_directories",
        "unknown_directories",
        "changes",
        "change_counts",
        "total",
        "full_total",
        "max_depth",
        "boundary_count",
        "skipped_directory_count",
        "mode",
    ],
)
def test_v2_validator_rejects_redundant_field_tampering(
    tmp_path: Path,
    field: str,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    candidate = scan_dirs.build_candidate_state(root)

    with pytest.raises(scan_dirs.ScanStateError):
        scan_dirs.validate_scan_state_v2(_tamper_candidate(candidate, field))


def test_candidate_generation_covers_non_snapshot_candidate_fields(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    candidate = scan_dirs.build_candidate_state(root)
    tampered = copy.deepcopy(candidate)
    tampered["scan_time"] = "2030-01-01T00:00:00+00:00"

    with pytest.raises(scan_dirs.ScanStateError, match="candidate_generation_id"):
        scan_dirs.validate_scan_state_v2(tampered)


def test_tampered_or_substituted_candidate_cannot_advance_baseline(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    direct_file = _mkdir_with_file(root / "package", text="AAAA")
    candidate_path = tmp_path / "candidate.json"
    state_path = tmp_path / "state.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    accepted = scan_dirs.build_candidate_state(
        root,
        baseline_precondition=absent,
    )
    state_path.write_text('{"sentinel": true}\n', encoding="utf-8")

    tampered = _tamper_candidate(accepted, "directories")
    with pytest.raises(scan_dirs.ScanStateError):
        scan_dirs.write_candidate_state(candidate_path, tampered)
    contract.atomic_write_json(candidate_path, tampered)
    with pytest.raises(scan_dirs.ScanStateError):
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=accepted["candidate_generation_id"],
        )
    assert contract.read_json_strict(state_path) == {"sentinel": True}

    direct_file.write_text("BBBB", encoding="utf-8")
    replacement = scan_dirs.build_candidate_state(root)
    scan_dirs.write_candidate_state(candidate_path, replacement)
    with pytest.raises(scan_dirs.ScanStateError, match="accepted worklist"):
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=accepted["candidate_generation_id"],
        )
    assert contract.read_json_strict(state_path) == {"sentinel": True}


def test_committed_state_is_portable_and_rejects_another_project(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first-project"
    first_root.mkdir()
    _mkdir_with_file(first_root / "package")
    candidate_path = tmp_path / "candidate.json"
    state_path = tmp_path / "state.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    candidate = scan_dirs.build_candidate_state(
        first_root,
        baseline_precondition=absent,
    )
    scan_dirs.write_candidate_state(candidate_path, candidate)

    committed = scan_dirs.commit_candidate_state(
        candidate_path,
        state_path,
        project_root=first_root,
        expected_baseline=absent,
        expected_candidate_generation_id=candidate["candidate_generation_id"],
    )
    persisted_text = state_path.read_text(encoding="utf-8")

    assert committed["root"] == "."
    assert len(committed["project_identity"]) == 64
    assert "candidate_path" not in committed
    assert committed["state_path"] == state_path.name
    assert not Path(committed["state_path"]).is_absolute()
    assert first_root.resolve().as_posix() not in persisted_text.replace("\\", "/")

    second_root = tmp_path / "second-project"
    second_root.mkdir()
    _mkdir_with_file(second_root / "package")
    previous = scan_dirs.load_previous_state(state_path)
    with pytest.raises(scan_dirs.ScanStateError, match="different project root"):
        scan_dirs.build_candidate_state(second_root, previous, incremental=True)


def test_committed_validator_rejects_worklist_and_absolute_path_tampering(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    first = scan_dirs.build_candidate_state(
        root,
        baseline_precondition=absent,
    )
    scan_dirs.write_candidate_state(candidate_path, first)
    scan_dirs.commit_candidate_state(
        candidate_path,
        state_path,
        project_root=root,
        expected_baseline=absent,
        expected_candidate_generation_id=first["candidate_generation_id"],
    )
    previous, baseline, _ = scan_dirs.load_previous_state_with_precondition(state_path)
    zero_change = scan_dirs.build_candidate_state(
        root,
        previous,
        incremental=True,
        baseline_precondition=baseline,
    )
    scan_dirs.write_candidate_state(candidate_path, zero_change)
    committed = scan_dirs.commit_candidate_state(
        candidate_path,
        state_path,
        project_root=root,
        expected_baseline=baseline,
        expected_candidate_generation_id=zero_change["candidate_generation_id"],
    )
    assert zero_change["directories"] == []
    assert committed["directories"] == committed["snapshot"]["directories"]

    bad_worklist = copy.deepcopy(committed)
    bad_worklist["directories"] = []
    bad_worklist["total"] = 0
    with pytest.raises(scan_dirs.ScanStateError, match="directories"):
        scan_dirs.validate_scan_state_v2(bad_worklist)

    for field, value in (
        ("root", root.resolve().as_posix()),
        ("state_path", state_path.resolve().as_posix()),
        ("candidate_path", candidate_path.resolve().as_posix()),
    ):
        tampered = copy.deepcopy(committed)
        tampered[field] = value
        with pytest.raises(scan_dirs.ScanStateError):
            scan_dirs.validate_scan_state_v2(tampered)


def test_legacy_absolute_root_v2_migrates_without_persisting_the_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    candidate = scan_dirs.build_candidate_state(root)
    legacy = copy.deepcopy(candidate)
    legacy["state_role"] = "committed"
    legacy["committed_at"] = "2026-07-29T00:00:00+00:00"
    legacy["directories"] = copy.deepcopy(legacy["snapshot"]["directories"])
    legacy["total"] = legacy["full_total"]
    legacy["root"] = root.resolve().as_posix()
    legacy.pop("project_identity")
    legacy.pop("candidate_generation_id")
    legacy_path = tmp_path / "legacy-v2.json"
    contract.atomic_write_json(legacy_path, legacy)

    migrated = scan_dirs.load_previous_state(legacy_path)
    assert migrated["schema_version"] == 1
    assert "root" not in migrated
    same_project = scan_dirs.build_candidate_state(root, migrated, incremental=True)
    assert same_project["changes"]["modified"] == ["", "package"]
    assert same_project["root"] == "."

    other_root = tmp_path / "other-project"
    other_root.mkdir()
    _mkdir_with_file(other_root / "package")
    with pytest.raises(scan_dirs.ScanStateError, match="different project root"):
        scan_dirs.build_candidate_state(other_root, migrated, incremental=True)


def test_force_full_ignores_previous_classification(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    changed_file = _mkdir_with_file(root / "stable", text="before")
    _mkdir_with_file(root / "gone")
    baseline = scan_dirs.build_candidate_state(root)
    changed_file.write_text("after", encoding="utf-8")
    (root / "gone" / "data.txt").unlink()
    (root / "gone").rmdir()
    _mkdir_with_file(root / "added")

    candidate = scan_dirs.build_candidate_state(
        root,
        baseline,
        incremental=True,
        force_full=True,
    )

    assert candidate["mode"] == "full"
    assert candidate["baseline"] == {
        "available": False,
        "schema_version": None,
        "snapshot_fingerprint": None,
        "candidate_generation_id": None,
        "force_full": True,
    }
    assert candidate["changes"] == {
        "new": ["", "added", "stable"],
        "modified": [],
        "deleted": [],
        "unchanged": [],
        "unknown": [],
    }
    assert {record["path"] for record in candidate["directories"]} == {
        "",
        "added",
        "stable",
    }
    ignored_invalid_baseline = scan_dirs.build_candidate_state(
        root,
        {"schema_version": 999, "directories": "invalid"},
        incremental=True,
        force_full=True,
    )
    assert ignored_invalid_baseline["changes"] == candidate["changes"]


def test_legacy_mtime_state_is_accepted_but_migrated_conservatively(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps(
            {
                "last_scan": "2026-07-29T00:00:00+08:00",
                "total": 1,
                "directories": [{"path": "package", "src_mtime": 0}],
            }
        ),
        encoding="utf-8",
    )

    legacy = scan_dirs.load_previous_state(legacy_path)
    candidate = scan_dirs.incremental_scan(root, legacy)

    assert candidate["baseline"] == {
        "available": True,
        "schema_version": 1,
        "snapshot_fingerprint": None,
        "candidate_generation_id": None,
        "force_full": False,
    }
    assert candidate["changes"]["modified"] == ["package"]
    assert candidate["schema_version"] == 2


def test_cli_json_is_clean_bare_output_works_and_errors_are_nonzero(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    script = Path(scan_dirs.__file__).resolve()
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"

    success = subprocess.run(
        [
            sys.executable,
            str(script),
            str(root),
            "--incremental",
            "--state",
            "committed.json",
            "--output",
            "candidate.json",
            "--json",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert success.returncode == 0, success.stderr
    payload = json.loads(success.stdout)
    assert payload["schema_version"] == 2
    assert payload["state_role"] == "candidate"
    assert success.stderr == ""
    assert (tmp_path / "candidate.json").is_file()
    assert not (tmp_path / "committed.json").exists()

    failure = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "missing"), "--json"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert failure.returncode != 0
    assert failure.stdout == ""
    assert "ERROR:" in failure.stderr


def test_cli_explicit_commit_promotes_generated_candidate(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    script = Path(scan_dirs.__file__).resolve()
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            str(root),
            "--state",
            str(state_path),
            "--candidate",
            str(candidate_path),
            "--commit",
            "--json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)
    assert output["commit"]["performed"] is True
    assert contract.read_json_strict(candidate_path)["state_role"] == "candidate"
    assert contract.read_json_strict(state_path)["state_role"] == "committed"


def test_stable_scan_retries_when_file_appears_after_first_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    package = root / "package"
    _mkdir_with_file(package, "first.txt", "first")

    def add_file_after_first_pass(point: str, _root: Path) -> None:
        if point == "after-pass-1":
            (package / "late.txt").write_text("late", encoding="utf-8")

    monkeypatch.setattr(scan_dirs, "_scan_fault", add_file_after_first_pass)
    candidate = scan_dirs.build_candidate_state(root)
    files = _snapshot_map(candidate)["package"]["snapshot"]["files"]

    assert [item["name"] for item in files] == ["first.txt", "late.txt"]


def test_stable_scan_rejects_mixed_sibling_snapshot_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    package = root / "package"
    first = _mkdir_with_file(package, "a.txt", "AAAA")
    (package / "b.txt").write_text("BBBB", encoding="utf-8")
    changed = False

    def mutate_between_passes(point: str, scan_root: Path) -> None:
        nonlocal changed
        if point == "after-pass-1" and not changed:
            changed = True
            first.write_text("CCCC", encoding="utf-8")

    # 注入点必须在两遍扫描之间（单线程），不能在另一个文件的快照钩子里改兄弟文件：
    # 同一目录下多文件走并发快照，那个写法会与并发读竞争，导致偶发的
    # "direct file changed while hashing" 而不是"混合快照 → 重试"。
    monkeypatch.setattr(scan_dirs, "_scan_fault", mutate_between_passes)
    candidate = scan_dirs.build_candidate_state(root)
    files = _snapshot_map(candidate)["package"]["snapshot"]["files"]
    by_name = {item["name"]: item for item in files}

    assert changed is True
    assert by_name["a.txt"]["sha256"] == hashlib.sha256(b"CCCC").hexdigest()


def test_stable_scan_detects_path_replacement_after_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    package = root / "package"
    payload = _mkdir_with_file(package, "payload.bin", "AAAA")
    original_snapshot = scan_dirs._file_snapshot
    replaced = False

    def replace_after_hash(entry, scan_root):
        nonlocal replaced
        snapshot = original_snapshot(entry, scan_root)
        if entry.name == "payload.bin" and not replaced:
            replaced = True
            replacement = package / "replacement.bin"
            replacement.write_text("BBBB", encoding="utf-8")
            os.replace(replacement, payload)
        return snapshot

    monkeypatch.setattr(scan_dirs, "_file_snapshot", replace_after_hash)
    candidate = scan_dirs.build_candidate_state(root)
    file_info = _snapshot_map(candidate)["package"]["snapshot"]["files"][0]

    assert replaced is True
    assert file_info["sha256"] == hashlib.sha256(b"BBBB").hexdigest()


def test_stable_scan_fails_after_bounded_continuous_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    payload = _mkdir_with_file(root / "package", "payload.txt", "0")
    passes: list[str] = []

    def mutate_after_every_pass(point: str, _root: Path) -> None:
        passes.append(point)
        payload.write_text(str(len(passes)), encoding="utf-8")

    monkeypatch.setattr(scan_dirs, "_scan_fault", mutate_after_every_pass)

    with pytest.raises(scan_dirs.ScanError, match="stable two-pass snapshot"):
        scan_dirs.build_candidate_state(root)
    assert passes == [
        "after-pass-1",
        "after-pass-2",
        "after-pass-3",
        "after-pass-4",
    ]


def test_candidate_commit_rejects_unbound_and_cross_project_candidates(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    _mkdir_with_file(first_root / "package")
    _mkdir_with_file(second_root / "package")
    candidate_path = tmp_path / "candidate.json"
    state_path = tmp_path / "state.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)

    unbound = scan_dirs.build_candidate_state(first_root)
    scan_dirs.write_candidate_state(candidate_path, unbound)
    with pytest.raises(scan_dirs.ScanStateError, match="not bound"):
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=first_root,
            expected_baseline=absent,
            expected_candidate_generation_id=unbound["candidate_generation_id"],
        )

    bound = scan_dirs.build_candidate_state(
        first_root,
        baseline_precondition=absent,
    )
    scan_dirs.write_candidate_state(candidate_path, bound)
    with pytest.raises(scan_dirs.ScanStateError, match="different project root"):
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=second_root,
            expected_baseline=absent,
            expected_candidate_generation_id=bound["candidate_generation_id"],
        )
    assert not state_path.exists()


def test_force_full_candidate_still_compares_exact_baseline_preimage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    initial = scan_dirs.build_candidate_state(root, baseline_precondition=absent)
    scan_dirs.write_candidate_state(candidate_path, initial)
    scan_dirs.commit_candidate_state(
        candidate_path,
        state_path,
        project_root=root,
        expected_baseline=absent,
        expected_candidate_generation_id=initial["candidate_generation_id"],
    )
    previous, baseline, _ = scan_dirs.load_previous_state_with_precondition(state_path)
    force_full = scan_dirs.build_candidate_state(
        root,
        previous,
        force_full=True,
        baseline_precondition=baseline,
    )
    scan_dirs.write_candidate_state(candidate_path, force_full)
    foreign = b'{"foreign":"replacement"}\n'
    state_path.write_bytes(foreign)

    with pytest.raises(scan_dirs.BaselineConflict, match="baseline changed"):
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=baseline,
            expected_candidate_generation_id=force_full["candidate_generation_id"],
        )
    assert state_path.read_bytes() == foreign


def test_candidate_commit_classifies_not_installed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    candidate = scan_dirs.build_candidate_state(root, baseline_precondition=absent)
    scan_dirs.write_candidate_state(candidate_path, candidate)

    def fail_before_install(_path: Path, _value: dict) -> Path:
        raise OSError("before-install")

    monkeypatch.setattr(contract, "atomic_write_json", fail_before_install)
    with pytest.raises(scan_dirs.CandidateCommitError) as caught:
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=candidate["candidate_generation_id"],
        )

    assert caught.value.disposition is scan_dirs.CommitDisposition.NOT_INSTALLED
    assert caught.value.durability_confirmed is False
    assert not state_path.exists()


def test_candidate_commit_classifies_installed_post_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    candidate = scan_dirs.build_candidate_state(root, baseline_precondition=absent)
    scan_dirs.write_candidate_state(candidate_path, candidate)
    real_atomic_write = contract.atomic_write_json

    def interrupt_after_install(path: Path, value: dict) -> Path:
        installed = real_atomic_write(path, value)
        raise KeyboardInterrupt("after-install")

    monkeypatch.setattr(contract, "atomic_write_json", interrupt_after_install)
    with pytest.raises(scan_dirs.CandidateCommitError) as caught:
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=candidate["candidate_generation_id"],
        )

    assert caught.value.disposition is scan_dirs.CommitDisposition.INSTALLED
    assert caught.value.durability_confirmed is False
    assert scan_dirs.load_previous_state(state_path) is not None


def test_candidate_commit_classifies_post_success_aba_as_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    candidate = scan_dirs.build_candidate_state(root, baseline_precondition=absent)
    scan_dirs.write_candidate_state(candidate_path, candidate)
    real_atomic_write = contract.atomic_write_json

    def install_then_restore_absent(path: Path, value: dict) -> Path:
        installed = real_atomic_write(path, value)
        path.unlink()
        return installed

    monkeypatch.setattr(contract, "atomic_write_json", install_then_restore_absent)
    with pytest.raises(scan_dirs.CandidateCommitError) as caught:
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=candidate["candidate_generation_id"],
        )

    assert caught.value.disposition is scan_dirs.CommitDisposition.UNKNOWN
    assert caught.value.durability_confirmed is True
    assert not state_path.exists()


def test_candidate_commit_classifies_foreign_bytes_as_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    state_path = tmp_path / "state.json"
    candidate_path = tmp_path / "candidate.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    candidate = scan_dirs.build_candidate_state(root, baseline_precondition=absent)
    scan_dirs.write_candidate_state(candidate_path, candidate)
    foreign = b'{"foreign":true}\n'

    def install_foreign_then_fail(path: Path, _value: dict) -> Path:
        path.write_bytes(foreign)
        raise OSError("foreign-install")

    monkeypatch.setattr(contract, "atomic_write_json", install_foreign_then_fail)
    with pytest.raises(scan_dirs.CandidateCommitError) as caught:
        scan_dirs.commit_candidate_state(
            candidate_path,
            state_path,
            project_root=root,
            expected_baseline=absent,
            expected_candidate_generation_id=candidate["candidate_generation_id"],
        )

    assert caught.value.disposition is scan_dirs.CommitDisposition.UNKNOWN
    assert caught.value.durability_confirmed is False
    assert state_path.read_bytes() == foreign


def test_scanner_rejects_indirect_root_and_output_parent(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real-project"
    real_root.mkdir()
    _mkdir_with_file(real_root / "package")
    root_link = tmp_path / "project-link"
    try:
        root_link.symlink_to(real_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(scan_dirs.ScanError, match="indirect path"):
        scan_dirs.build_candidate_state(root_link)

    candidate = scan_dirs.build_candidate_state(real_root)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    output_link.symlink_to(real_output, target_is_directory=True)
    with pytest.raises(scan_dirs.ScanStateError, match="direct real directory"):
        scan_dirs.write_candidate_state(output_link / "candidate.json", candidate)


def test_output_parent_is_not_created_through_an_indirect_ancestor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    candidate = scan_dirs.build_candidate_state(root)
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    output_link = tmp_path / "output-link"
    try:
        output_link.symlink_to(real_output, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    escaped_parent = real_output / "new" / "nested"
    with pytest.raises(scan_dirs.ScanStateError, match="existing output parent"):
        scan_dirs.write_candidate_state(
            output_link / "new" / "nested" / "candidate.json",
            candidate,
        )

    assert not escaped_parent.exists()


def test_state_target_lock_is_shared_across_project_roots(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    state_path = tmp_path / "shared-state.json"

    first_lock = scan_dirs._state_commit_lock_path(first_root, state_path)
    second_lock = scan_dirs._state_commit_lock_path(second_root, state_path)
    other_lock = scan_dirs._state_commit_lock_path(
        first_root,
        tmp_path / "other-state.json",
    )

    assert first_lock == second_lock
    assert first_lock != other_lock
    assert first_lock.parent == contract.project_sync_lock_path(first_root).parent


def test_cli_existing_candidate_requires_and_uses_explicit_baseline_preimage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _mkdir_with_file(root / "package")
    script = Path(scan_dirs.__file__).resolve()
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    candidate_path = tmp_path / "candidate.json"
    state_path = tmp_path / "state.json"
    absent = scan_dirs.BaselinePrecondition(False, None, None)
    candidate = scan_dirs.build_candidate_state(root, baseline_precondition=absent)
    scan_dirs.write_candidate_state(candidate_path, candidate)
    base_command = [
        sys.executable,
        str(script),
        str(root),
        "--state",
        str(state_path),
        "--commit-candidate",
        str(candidate_path),
        "--expected-candidate-generation-id",
        candidate["candidate_generation_id"],
        "--json",
    ]

    missing_preimage = subprocess.run(
        base_command,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert missing_preimage.returncode != 0
    assert "expected-baseline" in missing_preimage.stderr
    assert not state_path.exists()

    committed = subprocess.run(
        [*base_command, "--expected-baseline-absent"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert committed.returncode == 0, committed.stderr
    assert json.loads(committed.stdout)["state_role"] == "committed"
    assert scan_dirs.load_previous_state(state_path) is not None
