"""Local load shedding must not open the Redis circuit breaker (no Redis needed)."""
import asyncio

from ram_online.core import OnlineOverloaded, OnlineUnavailable
from ram_online.production import CircuitBreaker, FailOpenGate


class Gate:
    def __init__(self):
        self.mode = "overload"

    async def check_call_allowed_with_version(self, phone):
        if self.mode == "overload":
            raise OnlineOverloaded("online capacity exceeded")
        if self.mode == "redis_down":
            raise OnlineUnavailable("Redis unavailable")
        return False, "v1"          # number is on the deny list


def test_overload_burst_does_not_open_breaker_for_everyone():
    async def scenario():
        gate = Gate()
        adapter = FailOpenGate(gate, CircuitBreaker(failure_threshold=2, cooldown=60))
        for _ in range(10):
            shed = await adapter.check_call_allowed("+84901234567")
            assert shed.status == "UNKNOWN"
        assert adapter.breaker.state == "closed"     # before the fix: open after 2 shed requests
        assert shed.reason == "online_overloaded"
        gate.mode = "ok"
        blocked = await adapter.check_call_allowed("+84901234567")
        assert blocked.status == "BLOCK"          # before the fix: circuit open -> ALLOW for 60 s
        gate.mode = "redis_down"
        for _ in range(2):
            await adapter.check_call_allowed("+84901234567")
        assert adapter.breaker.state == "open"    # real dependency failures still open it
    asyncio.run(scenario())


def test_shed_half_open_probe_lets_next_request_probe():
    clock = [0.0]
    breaker = CircuitBreaker(failure_threshold=1, cooldown=1.0, clock=lambda: clock[0])
    breaker.failure(breaker.try_enter())
    clock[0] = 2.0
    probe = breaker.try_enter()
    assert probe.probe
    breaker.released(probe)
    assert breaker.try_enter() is not None
