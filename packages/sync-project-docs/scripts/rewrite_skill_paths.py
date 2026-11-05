# -*- coding: utf-8 -*-
"""Rewrite project-local skill paths to the bundle-relative <skill-dir> form.

Run once from the package directory:  python scripts/rewrite_skill_paths.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "skills" / "sync-project-docs"
FILES = [ROOT / "SKILL.md", *sorted((ROOT / "references").glob("*.md"))]
OLD = ".dsh/skills/sync-project-docs/"
NEW = "<skill-dir>/"
CMD = re.compile(r'python (<skill-dir>/tools/[A-Za-z0-9_./]+\.py)')


def main() -> int:
    total = 0
    for path in FILES:
        text = path.read_text(encoding="utf-8", newline="")
        before = text
        text = text.replace(OLD, NEW)
        text = CMD.sub(r'python "\1"', text)
        if text != before:
            path.write_text(text, encoding="utf-8", newline="")
            total += 1
            print(f"rewrote {path.relative_to(ROOT.parent)}")
    print(f"{total} file(s) updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
