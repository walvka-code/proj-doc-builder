# -*- coding: utf-8 -*-
"""Platform-specific filesystem handles, junctions, locks, and durability synchronization."""

from __future__ import annotations

import phase4_exit as _core

fsync_directory = getattr(_core, "_fsync_directory", None)
fsync_directory_windows = getattr(_core, "_fsync_directory_windows", None)
identity_from_stat = getattr(_core, "_identity_from_stat", None)
read_stable_regular_fd = getattr(_core, "_read_stable_regular_fd", None)
rename_no_replace = getattr(_core, "_rename_no_replace", None)

__all__ = [
    "fsync_directory",
    "fsync_directory_windows",
    "identity_from_stat",
    "read_stable_regular_fd",
    "rename_no_replace",
]
