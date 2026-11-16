"""Per-process asyncio factories for precise Windows deadlines.

Python 3.12 on Windows exposes a coarse GetTickCount64 monotonic clock.
Use QueryPerformanceCounter for this loop's clock and timer resolution.
No global Windows timer resolution or process-wide asyncio policy is changed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from time import get_clock_info, perf_counter

class _PreciseClock:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clock_resolution = get_clock_info("perf_counter").resolution

    def time(self):
        return perf_counter()

class PreciseSelectorEventLoop(_PreciseClock, asyncio.SelectorEventLoop):
    pass

if hasattr(asyncio, "ProactorEventLoop"):
    class PreciseProactorEventLoop(_PreciseClock, asyncio.ProactorEventLoop):
        pass
else:
    PreciseProactorEventLoop = None

def select_loop_factory(use_subprocess=False):
    """Select a platform loop constructor without changing the global policy."""
    windows = sys.platform == "win32"
    precise = windows and os.environ.get("FRAUD_PRECISE_EVENT_LOOP", "1") != "0"
    selected = os.environ.get("FRAUD_WINDOWS_EVENT_LOOP", "proactor")
    if selected not in ("proactor", "selector"):
        raise ValueError("FRAUD_WINDOWS_EVENT_LOOP must be proactor or selector")
    if windows and not use_subprocess and selected == "proactor":
        return PreciseProactorEventLoop if precise else asyncio.ProactorEventLoop
    return PreciseSelectorEventLoop if precise else asyncio.SelectorEventLoop

def uvicorn_loop_factory():
    """Uvicorn custom factories construct a loop, unlike its built-in selectors."""
    return select_loop_factory()()

def run_async(coroutine):
    with asyncio.Runner(loop_factory=uvicorn_loop_factory) as runner:
        return runner.run(coroutine)

def clock_description():
    return {
        "factory": "precise_event_loop:uvicorn_loop_factory",
        "loop_class": select_loop_factory().__name__,
        "windows_event_loop": os.environ.get("FRAUD_WINDOWS_EVENT_LOOP", "proactor"),
        "precise_windows_clock": sys.platform == "win32" and os.environ.get("FRAUD_PRECISE_EVENT_LOOP", "1") != "0",
        "standard_monotonic_resolution_s": get_clock_info("monotonic").resolution,
        "performance_counter_resolution_s": get_clock_info("perf_counter").resolution,
    }
