# -*- coding: utf-8 -*-
"""Content-Addressable Storage (CAS), payload snapshots, receipts, and hash verification."""

from __future__ import annotations

import phase4_exit as _core

text_snapshot = getattr(_core, "_text_snapshot", None)
write_exclusive_snapshot = getattr(_core, "_write_exclusive_snapshot", None)
validate_manifest_snapshot = getattr(_core, "_validate_manifest_snapshot", None)
require_receipt_closed_state = getattr(_core, "_require_receipt_closed_state", None)

__all__ = [
    "text_snapshot",
    "write_exclusive_snapshot",
    "validate_manifest_snapshot",
    "require_receipt_closed_state",
]
