# -*- coding: utf-8 -*-
"""Tests for exact, idempotent Last synced marker normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

import normalize_markers as normalizer

BAD = (
    "<!-- Last synced: 2026-07-23T16:32:05+08:00 "
    "by proj-doc-builder v3.2 --> v3.1 -->"
)
GOOD = "<!-- Last synced: 2026-07-23T16:32:05+08:00 " "by proj-doc-builder v3.2 -->"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_normalize_only_exact_historical_tail() -> None:
    content = "\n".join(
        [
            "# Doc",
            BAD,
            "自由文本 v3.1 --> 不应改变",
            "<!-- Last synced: malformed --> other -->",
        ]
    )
    normalized, changes = normalizer.normalize_marker_content(content, path="a.md")
    assert GOOD in normalized
    assert BAD not in normalized
    assert "自由文本 v3.1 --> 不应改变" in normalized
    assert "<!-- Last synced: malformed --> other -->" in normalized
    assert changes == [
        {
            "path": "a.md",
            "line": 2,
            "before": BAD,
            "after": GOOD,
        }
    ]


def test_normalization_preserves_crlf() -> None:
    content = f"# Doc\r\n{BAD}\r\nnext\r\n"
    normalized, changes = normalizer.normalize_marker_content(content)
    assert changes
    assert normalized == f"# Doc\r\n{GOOD}\r\nnext\r\n"


def test_normalization_is_idempotent() -> None:
    once, changes = normalizer.normalize_marker_content(BAD)
    twice, second_changes = normalizer.normalize_marker_content(once)
    assert changes
    assert once == GOOD
    assert twice == once
    assert second_changes == []


def test_collect_excludes_quarantine_and_apply_is_explicit(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    _write(root / "bad.md", f"# Bad\n{BAD}\n")
    _write(root / "ok.md", f"# OK\n{GOOD}\n")
    _write(root / ".quarantine" / "receipt" / "old.md", f"# Old\n{BAD}\n")

    changes = normalizer.collect_marker_changes(root)
    assert [(change.path, change.line) for change in changes] == [("bad.md", 2)]
    assert BAD in (root / "bad.md").read_text(encoding="utf-8")

    assert normalizer.apply_marker_changes(root, changes) == ["bad.md"]
    assert GOOD in (root / "bad.md").read_text(encoding="utf-8")
    assert normalizer.collect_marker_changes(root) == []
    assert BAD in (root / ".quarantine" / "receipt" / "old.md").read_text(
        encoding="utf-8"
    )


def test_apply_rejects_concurrent_change(tmp_path: Path) -> None:
    root = tmp_path / "project_docs"
    path = root / "bad.md"
    _write(path, f"# Bad\n{BAD}\n")
    changes = normalizer.collect_marker_changes(root)
    path.write_text("# changed concurrently\n", encoding="utf-8")

    try:
        normalizer.apply_marker_changes(root, changes)
    except normalizer.MarkerNormalizationError as exc:
        assert "已变化" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("concurrent modification must fail")
    assert path.read_text(encoding="utf-8") == "# changed concurrently\n"


def test_collect_rejects_inventory_to_read_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project_docs"
    victim = root / "a.md"
    replacement = root / "b.md"
    _write(victim, f"# A\n{BAD}\n")
    _write(replacement, f"# B\n{GOOD}\n")
    original_discover = normalizer.discover_documents

    def swapping_discover(project_docs: Path) -> list[tuple[str, Path]]:
        documents = original_discover(project_docs)
        try:
            victim.unlink()
            victim.symlink_to(replacement)
        except (NotImplementedError, OSError) as exc:
            pytest.skip(f"symlink unavailable: {exc}")
        return documents

    monkeypatch.setattr(normalizer, "discover_documents", swapping_discover)
    with pytest.raises(normalizer.ProjectDocsContractError):
        normalizer.collect_marker_changes(root)
