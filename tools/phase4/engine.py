# -*- coding: utf-8 -*-
"""Phase 4 engine execution: analysis, quarantine, restore, and coordinator integration."""

from __future__ import annotations

import phase4_exit as _core

analyze_project = _core.analyze_project
quarantine_candidates = _core.quarantine_candidates
restore_receipt = _core.restore_receipt
recover_pending_transactions = _core.recover_pending_transactions
main = _core.main

__all__ = [
    "analyze_project",
    "quarantine_candidates",
    "restore_receipt",
    "recover_pending_transactions",
    "main",
]
