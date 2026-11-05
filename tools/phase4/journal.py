# -*- coding: utf-8 -*-
"""Crash-consistent Phase 4 transaction journal, state machine, and atomic recovery."""

from __future__ import annotations

import phase4_exit as _core

transaction_fault = getattr(_core, "_transaction_fault", None)
new_transaction_id = getattr(_core, "_new_transaction_id", None)
journal_base = getattr(_core, "_journal_base", None)
write_transaction_journal = getattr(_core, "_write_transaction_journal", None)
clear_transaction_journal = getattr(_core, "_clear_transaction_journal", None)
recover_single_transaction = getattr(_core, "_recover_single_transaction", None)

__all__ = [
    "transaction_fault",
    "new_transaction_id",
    "journal_base",
    "write_transaction_journal",
    "clear_transaction_journal",
    "recover_single_transaction",
]
