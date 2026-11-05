# -*- coding: utf-8 -*-
"""Tests for the shared-resolver link CLIs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import compute_link
import link_check


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _docs(tmp_path: Path) -> Path:
    root = tmp_path / "project_docs"
    _write(root / "index.md", "# Index\n\n[child](section/child)\n")
    _write(
        root / "section" / "child.md",
        "# Child\n\n[root](../index.md#top)\n[code](../../src/tool.py)\n",
    )
    return root


def test_compute_relative_link_accepts_legacy_project_docs_prefix(
    tmp_path: Path,
) -> None:
    root = _docs(tmp_path)
    assert (
        compute_link.compute_relative_link(
            "project_docs/section/child.md",
            "project_docs/index.md",
            str(root),
            must_exist=True,
        )
        == "../index.md"
    )


def test_batch_compute_validates_shape_and_uses_posix(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    result = compute_link.batch_compute(
        [{"from": "index.md", "to": "section/child.md"}],
        str(root),
        must_exist=True,
    )
    assert result == [
        {
            "from": "index.md",
            "to": "section/child.md",
            "link": "section/child.md",
        }
    ]
    with pytest.raises(compute_link.JsonContractError):
        compute_link.batch_compute([{"from": "index.md"}], str(root))


def test_validate_doc_links_reports_invalid_and_missing(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    _write(
        root / "bad.md",
        "# Bad\n\n[missing](missing)\n[outside](../../outside.md)\n",
    )
    issues = compute_link.validate_doc_links("bad.md", str(root))
    assert [(item["kind"], item["target"]) for item in issues] == [
        ("missing", "missing"),
        ("invalid", "../../outside.md"),
    ]


def test_link_check_is_read_only_and_excludes_quarantine(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    _write(root / ".quarantine" / "r" / "old.md", "# Old\n\n[bad](missing.md)\n")
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

    count, issues = link_check.check_links(root)

    after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))
    assert count == 0
    assert issues == []
    assert before == after
    assert not (root / "_manifest.json").exists()


def test_link_check_excludes_only_root_quarantine(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    _write(root / ".quarantine" / "r" / "old.md", "# Old\n\n[bad](missing.md)\n")
    nested = "section/.quarantine/current.md"
    _write(root / nested, "# Current\n\n[bad](missing.md)\n")

    count, issues = link_check.check_links(root)

    assert count == 1
    assert issues[0]["doc"] == nested


def test_link_check_reports_encoded_escape_and_missing(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    _write(
        root / "bad.md",
        "# Bad\n\n[missing](none)\n[escape](%2E%2E/%2E%2E/out.md)\n",
    )
    count, issues = link_check.check_links(root)
    assert count == 2
    assert {(item["kind"], item["target"]) for item in issues} == {
        ("missing", "none"),
        ("invalid", "%2E%2E/%2E%2E/out.md"),
    }


def test_link_check_json_stdout_is_single_document(tmp_path: Path, capsys) -> None:
    root = _docs(tmp_path)
    assert link_check.main(["--project-docs", str(root), "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["manifest_written"] is False


def test_link_check_reports_invalid_utf8_as_json_error(tmp_path: Path, capsys) -> None:
    root = _docs(tmp_path)
    (root / "corrupt.md").write_bytes(b"# valid\n\xff")
    assert link_check.main(["--project-docs", str(root), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "UTF-8" in payload["error"]
    assert not (root / "_manifest.json").exists()


def test_manifest_only_is_rejected_without_writing(tmp_path: Path, capsys) -> None:
    root = _docs(tmp_path)
    assert (
        link_check.main(["--project-docs", str(root), "--manifest-only", "--json"]) == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert not (root / "_manifest.json").exists()


def test_compute_link_json_mode_reports_mode_errors_cleanly(
    tmp_path: Path, capsys
) -> None:
    root = _docs(tmp_path)
    assert compute_link.main(["--project-docs", str(root), "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False


def test_batch_output_is_atomic_json(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    batch = tmp_path / "pairs.json"
    output = tmp_path / "result.json"
    batch.write_text(
        json.dumps({"pairs": [{"from": "index.md", "to": "section/child.md"}]}),
        encoding="utf-8",
    )
    assert (
        compute_link.main(
            [
                "--project-docs",
                str(root),
                "--batch",
                str(batch),
                "--output",
                str(output),
                "--must-exist",
            ]
        )
        == 0
    )
    assert (
        json.loads(output.read_text(encoding="utf-8"))[0]["link"] == "section/child.md"
    )


def test_link_check_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _docs(tmp_path)
    outside = tmp_path / "outside.md"
    _write(outside, "# Outside\n")
    link = root / "escape.md"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    with pytest.raises(link_check.PathValidationError):
        link_check.check_links(root)


def test_link_tools_reject_symlinked_docs_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real_docs"
    _write(real_root / "index.md", "# Index\n")
    linked_root = tmp_path / "project_docs"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(link_check.PathValidationError, match="root"):
        link_check.check_links(linked_root)
    with pytest.raises(link_check.PathValidationError, match="root"):
        compute_link.compute_relative_link(
            "index.md",
            "index.md",
            str(linked_root),
            must_exist=True,
        )


def test_link_check_rejects_inventory_to_read_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project_docs"
    victim = root / "a.md"
    replacement = root / "b.md"
    _write(victim, "# A\n\n[missing](missing.md)\n")
    _write(replacement, "# B\n")
    original_discover = link_check.discover_active_markdown_paths
    swapped = False

    def swapping_discover(root_value: Path) -> list[str]:
        nonlocal swapped
        paths = original_discover(root_value)
        if not swapped:
            try:
                victim.unlink()
                victim.symlink_to(replacement)
            except (NotImplementedError, OSError) as exc:
                pytest.skip(f"symlink unavailable: {exc}")
            swapped = True
        return paths

    monkeypatch.setattr(
        link_check,
        "discover_active_markdown_paths",
        swapping_discover,
    )
    with pytest.raises(link_check.ProjectDocsContractError):
        link_check.check_links(root)


def test_link_check_json_forces_utf8_under_ascii_environment(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    _write(root / "中文.md", "# 中文\n\n[缺失](不存在.md)\n")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "ascii"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(link_check.__file__)),
            "--project-docs",
            str(root),
            "--json",
        ],
        check=False,
        capture_output=True,
        env=environment,
    )

    payload = json.loads(completed.stdout.decode("utf-8"))
    assert completed.returncode == 1
    assert completed.stderr == b""
    assert payload["broken"][0]["doc"] == "中文.md"
