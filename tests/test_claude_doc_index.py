# -*- coding: utf-8 -*-
"""Tests for deterministic CLAUDE document-tree generation."""

from __future__ import annotations

from pathlib import Path

import pytest

import claude_doc_index as indexer

SYNCED = "2026-07-30T09:00:00+08:00"


def _manifest() -> dict:
    return {
        "schema_version": 2,
        "producer": {"name": "sync-project-docs", "version": "3.2"},
        "last_synced": SYNCED,
        "files": [
            {
                "path": "alpha/child.md",
                "title": "child — 子文档",
                "status": "🔶",
            },
            {
                "path": "alpha.md",
                "title": "alpha — 主模块",
                "status": "✅",
            },
            {
                "path": "Beta.md",
                "title": "Beta",
                "status": "⬜",
            },
        ],
    }


def _claude(newline: str = "\n") -> str:
    blocks = ["# CLAUDE.md", "manual-before"]
    for section in indexer.REQUIRED_SECTIONS:
        blocks.extend(
            [
                indexer.section_start(section),
                f"old-{section}",
                indexer.section_end(section),
                f"manual-after-{section}",
            ]
        )
    return newline.join(blocks) + newline


def test_render_tree_is_complete_and_deterministic() -> None:
    rendered = indexer.render_document_tree(_manifest())
    assert rendered.splitlines() == [
        "project_docs/",
        "├── alpha — 主模块 ✅",
        "│   └── child — 子文档 🔶",
        "└── Beta ⬜",
    ]
    assert indexer.render_document_tree(_manifest()) == rendered


def test_tree_merges_case_only_file_directory_spelling() -> None:
    manifest = _manifest()
    manifest["files"].extend(
        [
            {
                "path": "pkg/Worker_Node/RSA.md",
                "title": "RSA",
                "status": "✅",
            },
            {
                "path": "pkg/worker_node.md",
                "title": "Worker_Node — 装配测试",
                "status": "✅",
            },
        ]
    )
    rendered = indexer.render_document_tree(manifest)
    assert "Worker_Node — 装配测试 ✅" in rendered
    assert "└── RSA ✅" in rendered
    assert "worker_node — Worker_Node" not in rendered


def test_replace_changes_only_doc_tree_block() -> None:
    original = _claude()
    updated = indexer.replace_doc_tree_section(original, _manifest())

    assert "old-doc-tree-instruction" not in updated
    assert "old-project-overview" in updated
    assert "manual-before" in updated
    assert f"Last synced: {SYNCED}" in updated
    for section in indexer.REQUIRED_SECTIONS:
        assert updated.count(indexer.section_start(section)) == 1
        assert updated.count(indexer.section_end(section)) == 1


def test_replace_preserves_crlf_outside_replaced_block() -> None:
    original = _claude("\r\n")
    updated = indexer.replace_doc_tree_section(original, _manifest())
    assert "\r\nmanual-after-doc-tree-instruction\r\n" in updated
    assert "\n" in updated
    assert updated.replace("\r\n", "").find("\n") == -1


def test_marker_missing_duplicate_and_reversed_fail_closed() -> None:
    original = _claude()
    with pytest.raises(indexer.ClaudeIndexError, match="START=0"):
        indexer.validate_required_sections(
            original.replace(indexer.section_start("architecture"), "")
        )
    with pytest.raises(indexer.ClaudeIndexError, match="START=2"):
        indexer.validate_required_sections(
            original + indexer.section_start("architecture")
        )
    start = indexer.section_start("dependencies")
    end = indexer.section_end("dependencies")
    reversed_content = original.replace(
        f"{start}\nold-dependencies\n{end}",
        f"{end}\nold-dependencies\n{start}",
    )
    with pytest.raises(indexer.ClaudeIndexError, match="precedes"):
        indexer.validate_required_sections(reversed_content)


def test_update_is_dry_run_by_default_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "CLAUDE.md"
    path.write_text(_claude(), encoding="utf-8")
    before = path.read_bytes()

    dry = indexer.update_claude_doc_index(path, _manifest())
    assert dry["changed"] is True
    assert path.read_bytes() == before

    applied = indexer.update_claude_doc_index(path, _manifest(), apply=True)
    assert applied["changed"] is True
    second = indexer.update_claude_doc_index(path, _manifest())
    assert second["changed"] is False


def test_render_tree_pins_index_first() -> None:
    manifest = _manifest()
    manifest["files"].append(
        {
            "path": "index.md",
            "title": "index — 项目全息总览",
            "status": "✅",
        }
    )
    rendered = indexer.render_document_tree(manifest)
    lines = rendered.splitlines()
    assert lines[0] == "project_docs/"
    assert lines[1] == "├── index — 项目全息总览 ✅"
    assert "├── alpha — 主模块 ✅" in lines

