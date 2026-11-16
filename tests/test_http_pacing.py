import asyncio
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import http_loadtest
import pytest


@pytest.mark.parametrize("strategy", ["thread_sleep", "asyncio_sleep"])
def test_arrivals_never_release_before_the_original_scheduled_time(monkeypatch, strategy):
    monkeypatch.setattr(http_loadtest, "ARRIVAL_PACING", strategy)
    async def check():
        target = time.perf_counter() + .005
        await http_loadtest.wait_until(target)
        assert time.perf_counter() >= target
        await http_loadtest.wait_until(target - 1)
    asyncio.run(check())


def test_thread_pacing_keeps_event_loop_responsive(monkeypatch):
    monkeypatch.setattr(http_loadtest, "ARRIVAL_PACING", "thread_sleep")
    async def responsive():
        gate = threading.Event()
        original_to_thread = asyncio.to_thread
        async def timer_thread(function, delay):
            def wait():
                assert gate.wait(2)
                function(delay)
            return await original_to_thread(wait)
        monkeypatch.setattr(http_loadtest.asyncio, "to_thread", timer_thread)
        task = asyncio.create_task(http_loadtest.wait_until(time.perf_counter() + .005))
        await asyncio.sleep(0)
        gate.set()
        await task
    asyncio.run(responsive())
