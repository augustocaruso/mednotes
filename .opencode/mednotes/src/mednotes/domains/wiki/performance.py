"""Small cooperative pacing helpers for long interactive wiki workflows."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

SleepFn = Callable[[float], None]


@dataclass(frozen=True)
class CooperativeCpuYieldSettings:
    enabled: bool
    every: int
    seconds: float


_CPU_YIELD_ENABLED: ContextVar[bool] = ContextVar("mednotes_cpu_yield_enabled", default=False)
_CPU_YIELD_EVERY: ContextVar[int] = ContextVar("mednotes_cpu_yield_every", default=32)
_CPU_YIELD_SECONDS: ContextVar[float] = ContextVar("mednotes_cpu_yield_seconds", default=0.0)
_CPU_YIELD_SLEEP: ContextVar[SleepFn] = ContextVar("mednotes_cpu_yield_sleep", default=time.sleep)


def _positive_int_from_env(name: str, default: int) -> int:
    import os

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _non_negative_float_from_env(name: str, default: float) -> float:
    import os

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(0.0, value)


def cooperative_cpu_yield_settings_from_env(
    *,
    default_enabled: bool,
    default_every: int = 2,
    default_seconds: float = 0.0025,
) -> CooperativeCpuYieldSettings:
    import os

    enabled = default_enabled and os.environ.get("MEDNOTES_CPU_YIELD_DISABLED") != "1"
    return CooperativeCpuYieldSettings(
        enabled=enabled,
        every=_positive_int_from_env("MEDNOTES_CPU_YIELD_EVERY", default_every),
        seconds=_non_negative_float_from_env("MEDNOTES_CPU_YIELD_SECONDS", default_seconds),
    )


@contextmanager
def cooperative_cpu_yield_scope(
    *,
    enabled: bool,
    every: int = 32,
    seconds: float = 0.005,
    sleep: SleepFn = time.sleep,
) -> Iterator[None]:
    enabled_token = _CPU_YIELD_ENABLED.set(enabled)
    every_token = _CPU_YIELD_EVERY.set(max(1, int(every or 1)))
    seconds_token = _CPU_YIELD_SECONDS.set(max(0.0, float(seconds or 0.0)))
    sleep_token = _CPU_YIELD_SLEEP.set(sleep)
    try:
        yield
    finally:
        _CPU_YIELD_SLEEP.reset(sleep_token)
        _CPU_YIELD_SECONDS.reset(seconds_token)
        _CPU_YIELD_EVERY.reset(every_token)
        _CPU_YIELD_ENABLED.reset(enabled_token)


def cooperative_cpu_yield(index: int) -> None:
    if not _CPU_YIELD_ENABLED.get():
        return
    seconds = _CPU_YIELD_SECONDS.get()
    if seconds <= 0:
        return
    every = _CPU_YIELD_EVERY.get()
    if index > 0 and index % every == 0:
        _CPU_YIELD_SLEEP.get()(seconds)
