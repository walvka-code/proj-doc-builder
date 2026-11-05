# -*- coding: utf-8 -*-
"""Phase 4 modular subpackage providing structured access to Phase 4 subsystems.

Submodules:
- ``platform_handles``: Platform-specific Win32/POSIX handles, locks, reparse points, and fsync.
- ``cas_storage``: Content-Addressable Storage, SHA-256 fingerprinting, receipts, and payloads.
- ``journal``: Crash-consistent transaction journal, state machine, and recovery.
- ``engine``: Candidate analysis, quarantine execution, receipt restoration, and coordinator integration.
"""

from __future__ import annotations

import phase4_exit as _core

# Re-export core constants, classes, and entrypoints
EXIT_OK = _core.EXIT_OK
EXIT_REFUSED = _core.EXIT_REFUSED
EXIT_OPERATIONAL_FAILURE = _core.EXIT_OPERATIONAL_FAILURE

AUTO_MARKER = _core.AUTO_MARKER
QUARANTINE_DIRNAME = _core.QUARANTINE_DIRNAME
RECEIPT_FILENAME = _core.RECEIPT_FILENAME
PAYLOAD_DIRNAME = _core.PAYLOAD_DIRNAME
QUARANTINE_REASON = _core.QUARANTINE_REASON

Phase4Error = _core.Phase4Error
Phase4Refusal = _core.Phase4Refusal
Phase4OperationalError = _core.Phase4OperationalError
ReceiptRecord = _core.ReceiptRecord
ProjectAnalysis = _core.ProjectAnalysis

analyze_project = _core.analyze_project
quarantine_candidates = _core.quarantine_candidates
restore_receipt = _core.restore_receipt
recover_pending_transactions = _core.recover_pending_transactions
main = _core.main

__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_OPERATIONAL_FAILURE",
    "AUTO_MARKER",
    "QUARANTINE_DIRNAME",
    "RECEIPT_FILENAME",
    "PAYLOAD_DIRNAME",
    "QUARANTINE_REASON",
    "Phase4Error",
    "Phase4Refusal",
    "Phase4OperationalError",
    "ReceiptRecord",
    "ProjectAnalysis",
    "analyze_project",
    "quarantine_candidates",
    "restore_receipt",
    "recover_pending_transactions",
    "main",
]
