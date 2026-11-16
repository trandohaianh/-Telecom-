import asyncio
import sys
import time
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import precise_event_loop as clocks

def test_precise_selector_timer_does_not_fire_early():
    loop = clocks.PreciseSelectorEventLoop()
    async def exercise():
        began = time.perf_counter()
        await asyncio.sleep(.01)
        return time.perf_counter() - began
    try:
        assert .008 <= loop.run_until_complete(exercise()) < .5
    finally:
        loop.close()

def test_runner_closes_loop_after_success():
    captured = []
    async def exercise():
        captured.append(asyncio.get_running_loop())
        await asyncio.sleep(0)
        return 17
    assert clocks.run_async(exercise()) == 17
    assert captured[0].is_closed()

def test_runner_closes_loop_when_coroutine_raises():
    captured = []
    async def exercise():
        captured.append(asyncio.get_running_loop())
        raise RuntimeError("expected")
    with pytest.raises(RuntimeError, match="expected"):
        clocks.run_async(exercise())
    assert captured[0].is_closed()

def test_uvicorn_custom_factory_runs_a_coroutine():
    import uvicorn
    factory = uvicorn.Config("server:app", loop="precise_event_loop:uvicorn_loop_factory").get_loop_factory()
    loop = factory()
    try:
        assert loop.run_until_complete(asyncio.sleep(0, result="ready")) == "ready"
    finally:
        loop.close()

def test_clock_override_has_a_process_local_opt_out(monkeypatch):
    monkeypatch.setenv("FRAUD_PRECISE_EVENT_LOOP", "0")
    factory = clocks.select_loop_factory()
    expected = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop
    assert factory is expected
    assert clocks.clock_description()["precise_windows_clock"] is False


def test_explicit_selector_profile_uses_local_precise_clock(monkeypatch):
    monkeypatch.setenv("FRAUD_WINDOWS_EVENT_LOOP", "selector")
    monkeypatch.setenv("FRAUD_PRECISE_EVENT_LOOP", "1")
    expected = clocks.PreciseSelectorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop
    assert clocks.select_loop_factory() is expected
    assert clocks.clock_description()["windows_event_loop"] == "selector"


def test_invalid_loop_selection_fails_explicitly(monkeypatch):
    monkeypatch.setenv("FRAUD_WINDOWS_EVENT_LOOP", "typo")
    with pytest.raises(ValueError, match="FRAUD_WINDOWS_EVENT_LOOP"):
        clocks.select_loop_factory()
