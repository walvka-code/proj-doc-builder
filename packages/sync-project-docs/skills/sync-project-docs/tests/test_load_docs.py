# -*- coding: utf-8 -*-
"""Loader tests: verify-then-serve behaviour of ``load_docs``."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import load_docs
import project_docs_contract as contract

SYNCED = "2026-07-30T12:00:00+00:00"
LOAD_TOOL = Path(__file__).resolve().parents[1] / "tools" / "load_docs.py"


def _write_doc(docs_root: Path, relative: str, title: str, status: str = "✅") -> None:
    path = docs_root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"# {title}",
                "",
                "<!-- AUTO-GENERATED v3.0: folder-doc START -->",
                f"<!-- Last synced: {SYNCED} by sync-project-docs v3.2 -->",
                "",
                "**导航**: project_docs > **" + title.split(" — ")[0] + "**",
                "",
                "## 概述",
                "",
                f"{title} 用于验证加载器的行为，内容足够长以通过文档质量校验。",
                "",
                "## 依赖关系",
                "",
                "| 方向 | 模块 | 关系 | 文档 |",
                "|------|------|------|------|",
                "| ⬆ 上游 | index | 示例上游依赖说明文字 | [index.md](index.md) |",
                "",
                "## 状态",
                "",
                status,
                "",
                "<!-- AUTO-GENERATED v3.0: folder-doc END -->",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    docs = project / "project_docs"
    docs.mkdir(parents=True)
    _write_doc(docs, "index.md", "index — 工程总览")
    _write_doc(docs, "pkg.md", "pkg — 示例包")
    _write_doc(docs, "pkg/sub.md", "sub — 子模块")
    _publish_manifest(docs)
    return project


def _publish_manifest(docs: Path) -> dict:
    manifest = contract.build_manifest(
        docs,
        last_synced=SYNCED,
        docs_root_label="project_docs",
    )
    contract.atomic_write_json(docs / contract.MANIFEST_FILENAME, manifest)
    return manifest


def test_overview_reports_verified_project_shape(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(project)

    assert exit_code == load_docs.EXIT_OK
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["mode"] == "overview"
    assert result["counts"] == {"documents": 3, "returned": 3}
    assert result["manifest"]["total"] == 3
    assert "project_docs/" in result["tree"]
    paths = [item["path"] for item in result["documents"]]
    assert paths == ["index.md", "pkg.md", "pkg/sub.md"]
    assert "概述" in result["documents"][0]["sections"]
    assert "状态" in result["documents"][0]["sections"]


def test_overview_honours_limit_and_reports_omitted(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(project, limit=1)

    assert exit_code == load_docs.EXIT_OK
    assert result["counts"] == {"documents": 3, "returned": 1}
    assert result["omitted"]["documents"] == 2


def test_overview_can_drop_sections_and_tree(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(project, sections=0, include_tree=False)

    assert exit_code == load_docs.EXIT_OK
    assert "tree" not in result
    assert all("sections" not in item for item in result["documents"])


def test_target_resolves_source_directory_to_document(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(project, mode="target", requested="pkg")

    assert exit_code == load_docs.EXIT_OK
    assert result["mode"] == "target"
    assert result["doc"]["path"] == "pkg.md"
    assert "# pkg — 示例包" in result["content"]
    assert "状态" in result["available_sections"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    (
        ("pkg.md", "pkg.md"),
        ("project_docs/pkg.md", "pkg.md"),
        ("./pkg.md", "pkg.md"),
        ("pkg/", "pkg.md"),
        ("", "index.md"),
        (".", "index.md"),
    ),
)
def test_target_normalizes_accepted_path_forms(
    tmp_path: Path, requested: str, expected: str
) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(project, mode="target", requested=requested)

    assert exit_code == load_docs.EXIT_OK
    assert result["doc"]["path"] == expected


def test_target_section_returns_only_that_section(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(
        project, mode="target", requested="pkg", section="状态"
    )

    assert exit_code == load_docs.EXIT_OK
    assert result["section"] == "状态"
    assert result["content"].startswith("✅")
    assert "## 概述" not in result["content"]


def test_target_unknown_section_lists_available_sections(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(
        project, mode="target", requested="pkg", section="不存在的章节"
    )

    assert exit_code == load_docs.EXIT_STALE_OR_MISSING
    assert result["errors"][0]["code"] == "section-not-found"
    assert "概述" in result["available_sections"]


def test_target_refuses_unregistered_document(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(project, mode="target", requested="nope")

    assert exit_code == load_docs.EXIT_STALE_OR_MISSING
    assert result["errors"][0]["code"] == "target-not-registered"


def test_target_rejects_path_escape(tmp_path: Path) -> None:
    project = _project(tmp_path)

    result, exit_code = load_docs.execute(
        project, mode="target", requested="../../etc/passwd"
    )

    assert exit_code == load_docs.EXIT_INPUT_ERROR
    assert result["errors"][0]["code"] == "unsafe-target"


def test_stale_manifest_is_not_served(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "project_docs" / "pkg.md").write_text("# pkg — 被改过\n", encoding="utf-8")

    result, exit_code = load_docs.execute(project)

    assert exit_code == load_docs.EXIT_STALE_OR_MISSING
    assert result["ok"] is False
    assert result["verified"] is False
    assert result["errors"][0]["code"] == "manifest-stale"


def test_unregistered_extra_document_is_not_served(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _write_doc(project / "project_docs", "extra.md", "extra — 未注册")

    result, exit_code = load_docs.execute(project)

    assert exit_code == load_docs.EXIT_STALE_OR_MISSING
    assert result["errors"][0]["code"] == "manifest-stale"


def test_missing_manifest_reports_actionable_error(tmp_path: Path) -> None:
    project = tmp_path / "bare"
    (project / "project_docs").mkdir(parents=True)

    result, exit_code = load_docs.execute(project)

    assert exit_code == load_docs.EXIT_STALE_OR_MISSING
    assert result["errors"][0]["code"] == "manifest-missing"


def test_cli_json_is_single_document_and_signals_failures(tmp_path: Path) -> None:
    project = _project(tmp_path)
    environment = {"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}

    completed = subprocess.run(
        [sys.executable, str(LOAD_TOOL), str(project), "--json"],
        capture_output=True,
        check=False,
        env={**environment, "PATH": ""},
    )
    assert completed.returncode == load_docs.EXIT_OK, completed.stderr.decode("utf-8")
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert payload["ok"] is True
    assert payload["exit_code"] == load_docs.EXIT_OK

    missing = subprocess.run(
        [sys.executable, str(LOAD_TOOL), str(project), "--mode", "target", "--target", "nope", "--json"],
        capture_output=True,
        check=False,
        env={**environment, "PATH": ""},
    )
    assert missing.returncode == load_docs.EXIT_STALE_OR_MISSING
    failure = json.loads(missing.stdout.decode("utf-8"))
    assert failure["errors"][0]["code"] == "target-not-registered"
