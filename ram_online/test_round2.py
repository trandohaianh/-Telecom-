"""Round-2 audit regressions for ram_online (no Redis needed)."""

import asyncio

import pytest

from ram_online.production import CircuitBreaker, FailOpenGate
from ram_online.streaming import process_kafka_message


class Message:
    def __init__(self, value, key=b"+84901234567"):
        self._value, self._key = value, key

    def value(self):
        return self._value

    def key(self):
        return self._key

    def topic(self):
        return "t"

    def partition(self):
        return 0

    def offset(self):
        return 5


class Detector:
    async def process(self, event):
        return {"caller": event["caller"], "calls_60s": 1}


def test_malformed_record_is_dead_lettered_then_committed():
    async def scenario():
        dead, committed = [], []

        async def dead_letter(message, reason):
            dead.append(reason)

        async def commit(message):
            committed.append(message.offset())

        out = await process_kafka_message(
            Message(b"{not json"), Detector(), None, commit=commit, dead_letter=dead_letter
        )
        assert out["dead_lettered"] and dead and committed == [5]
        with pytest.raises(ValueError):  # without a DLQ the old fail-stop contract holds
            await process_kafka_message(Message(b"{not json"), Detector(), None, commit=commit)

    asyncio.run(scenario())


def test_infrastructure_errors_are_not_dead_lettered():
    class Down:
        async def process(self, event):
            raise ConnectionError("redis down")

    async def scenario():
        async def dead_letter(message, reason):
            raise AssertionError("transient failure must not be dead-lettered")

        with pytest.raises(ConnectionError):
            await process_kafka_message(
                Message(b'{"caller": "+84901234567"}'), Down(), None, commit=lambda m: None, dead_letter=dead_letter
            )

    asyncio.run(scenario())


def test_unexpected_gate_error_becomes_fail_open_decision():
    class Gate:
        async def check_call_allowed_with_version(self, phone):
            raise OSError("socket reset")

    async def scenario():
        adapter = FailOpenGate(Gate(), CircuitBreaker(failure_threshold=2, cooldown=60))
        decision = await adapter.check_call_allowed("+84901234567")
        assert decision.status == "UNKNOWN" and decision.reason == "policy_error"
        assert adapter.breaker.failures == 1

    asyncio.run(scenario())
