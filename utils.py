"""
 * FAAC Benchmark Suite — Shared Utilities
 * Copyright (C) 2026 Nils Schimmelmann
"""

import subprocess

def safe_run(cmd, **kwargs):
    """
    Centralized subprocess runner that enforces security and consistent defaults.
    """
    # Enforce shell=False for security
    kwargs["shell"] = False

    # We default to list for cmd, but shlex.split could be used if cmd was a string.
    # Given the codebase, it's always a list.
    return subprocess.run(cmd, **kwargs)
