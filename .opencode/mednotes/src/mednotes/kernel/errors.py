"""Framework process-exit codes and the typed-error hierarchy.

Domain-agnostic: plain exit codes plus a base exception that carries one. Lives
in the framework so the FSM kernel can raise/type its errors without importing
domain facades. Layering rule: framework <- domain <- adapters
(tools/audit/import_layering.py).
"""
from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_MISSING = 4
EXIT_IO = 5


class MedOpsError(Exception):
    """Base exception carrying a process exit code."""

    exit_code = EXIT_IO


class ValidationError(MedOpsError):
    exit_code = EXIT_VALIDATION


class MissingPathError(MedOpsError):
    exit_code = EXIT_MISSING


class CollisionError(MedOpsError):
    exit_code = EXIT_VALIDATION


class FileWriteError(MedOpsError):
    """Filesystem write failed after local retry/recovery attempts."""

    exit_code = EXIT_IO
