import asyncio
import json
import os
import time
import uuid
import pytest
from ram_online.core import BloomFilter, OnlineGate, OnlineUnavailable, PolicyStore, Snapshot, make_client, run
from ram_online.production import CallDecision
from ram_online.streaming import SlidingWindowDetector, process_kafka_message

def test_call_decision_exposes_unknown_for_fail_open_and_exact_allow_block():
    assert CallDecision(True, "unavailable", "hidden", "policy_unavailable").status == "UNKNOWN"
    assert CallDecision(True, "known", "hidden", "exact_policy").status == "ALLOW"
    assert CallDecision(False, "known", "blocked", "exact_policy").status == "BLOCK"


def test_bloom_no_false_negative_and_immutable():
    bloom = BloomFilter(2000)
    members = [f"+849{i:08d}" for i in range(2000)]
    bloom.add_many(members)
    bloom.freeze()
    assert all(number in bloom for number in members)
    with pytest.raises(RuntimeError):
        bloom.add("+84999999999")

async def with_store(function):
    client = make_client(os.environ["RAM_TEST_POLICY_URL"], io_timeout=1)
    analytics = make_client(os.environ["RAM_TEST_ANALYTICS_URL"], io_timeout=1)
    namespace = "test-" + uuid.uuid4().hex
    store = PolicyStore(client, namespace)
    gate = OnlineGate(store, capacity=100, budget_ms=200)
    try:
        await store.initialize_empty_policy()
        await function(store, gate, analytics, namespace)
    finally:
        await client.delete(store.manifest_key, store.deny_key)
        keys = [key async for key in analytics.scan_iter("{" + namespace + ":*")]
        if keys: await analytics.delete(*keys)
        await client.aclose()
        await analytics.aclose()

def test_stale_negative_and_unblock():
    async def check(store, gate, *_):
        phone = "+84901234567"
        await gate.refresh_snapshot()
        assert phone not in gate.snapshot.bloom
        assert await gate.check_call_allowed(phone)
        await store.set_blocked(phone)
        assert phone not in gate.snapshot.bloom
        assert not await gate.check_call_allowed(phone)
        await gate.refresh_snapshot()
        await store.set_blocked(phone, False)
        assert await gate.check_call_allowed(phone)
    run(with_store(check))

def test_false_positive_never_blocks_innocent():
    async def check(store, gate, *_):
        bloom = BloomFilter(100)
        bloom.bits[:] = bytes([255]) * len(bloom.bits)
        gate.snapshot = Snapshot(await store.client.get(store.manifest_key), bloom.freeze())
        assert "+84909999999" in gate.snapshot.bloom
        assert await gate.check_call_allowed("+84909999999")
    run(with_store(check))

@pytest.mark.parametrize("missing", ["manifest", "exact_set"])
def test_missing_policy_is_unknown(missing):
    async def check(store, gate, *_):
        await store.set_blocked("+84901234567")
        await gate.refresh_snapshot()
        await store.client.delete(store.manifest_key if missing=="manifest" else store.deny_key)
        with pytest.raises(OnlineUnavailable):
            await gate.check_call_allowed("+84901234567")
    run(with_store(check))

def test_mutation_during_scan_retains_safe_old_snapshot(monkeypatch):
    async def check(store, gate, *_):
        await gate.refresh_snapshot()
        previous, original = gate.snapshot, store.client.sscan
        async def scan(*args, **kwargs):
            result = await original(*args, **kwargs)
            await store.set_blocked("+84901234567")
            return result
        monkeypatch.setattr(store.client, "sscan", scan)
        assert not await gate.refresh_snapshot()
        assert gate.snapshot is previous
        assert not await gate.check_call_allowed("+84901234567")
    run(with_store(check))

def test_window_dedup_lateness_boundary_and_feedback(monkeypatch):
    async def check(store, gate, analytics, namespace):
        await gate.refresh_snapshot()
        detector = SlidingWindowDetector(analytics, store, threshold=2,
            namespace=namespace, activate_demo_blocks=True)
        phone, now = "+84901234567", time.time_ns()//1000000
        def event(cid, ts):return {"caller":phone,"call_id":cid,"setup_ts_ms":ts}
        assert (await detector.process(event("a",now)))["calls_60s"]==1
        assert (await detector.process(event("b",now-1000)))["calls_60s"]==2
        assert (await detector.process(event("b",now-1000)))["calls_60s"]==2
        assert (await detector.process(event("old",now-60000)))["calls_60s"]==2
        result = await detector.process(event("c",now))
        assert result["flagged"] and result["activated"]
        assert not await gate.check_call_allowed(phone)
        monkeypatch.setattr(time,"time_ns",lambda:(now+60000)*1000000)
        assert (await detector.process(event("next",now+60000)))["calls_60s"]==1
    run(with_store(check))

def test_late_result_is_not_successful_allow():
    class SlowClient:
        async def eval(self,*args):
            time.sleep(.02)
            return 0
    async def check():
        gate=OnlineGate(PolicyStore(SlowClient()),budget_ms=1)
        with pytest.raises(OnlineUnavailable,match="after the original deadline"):
            await gate.check_call_allowed("+84901234567")
        assert gate._inflight==0
    run(check())

def test_bounded_admission_and_cancel():
    class Client:
        async def eval(self,*args):await asyncio.Event().wait()
    async def check():
        gate=OnlineGate(PolicyStore(Client()),budget_ms=1000,max_inflight=1)
        task=asyncio.create_task(gate.check_call_allowed("+84901234567"))
        await asyncio.sleep(0)
        with pytest.raises(OnlineUnavailable,match="capacity"):
            await gate.check_call_allowed("+84901234567")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert gate._inflight==0
    run(check())

@pytest.mark.parametrize("fail",[False,True])
def test_kafka_commit_after_success_only(fail):
    events=[]
    class Message:
        def key(self):return b"+84901234567"
        def value(self):return json.dumps({"caller":"+84901234567"}).encode()
    class Detector:
        async def process(self,event):
            events.append("processed")
            if fail:raise RuntimeError("sink failed")
            return {"flagged":True}
    class Consumer:
        def commit(self,**kwargs):
            assert kwargs["asynchronous"] is False
            events.append("committed")
    async def check():
        if fail:
            with pytest.raises(RuntimeError):
                await process_kafka_message(Message(),Detector(),Consumer())
            assert events==["processed"]
        else:
            await process_kafka_message(Message(),Detector(),Consumer())
            assert events==["processed","committed"]
    run(check())


def test_window_rejects_call_id_with_changed_setup_timestamp():
    async def check(store, gate, analytics, namespace):
        detector=SlidingWindowDetector(analytics,store,threshold=2,namespace=namespace)
        now=time.time_ns()//1000000
        event={"caller":"+84901234567","call_id":"same","setup_ts_ms":now}
        assert (await detector.process(event))["calls_60s"]==1
        from redis.exceptions import ResponseError
        with pytest.raises(ResponseError,match="timestamp conflict"):
            await detector.process({**event,"setup_ts_ms":now+1})
        assert (await detector.process(event))["calls_60s"]==1
    run(with_store(check))

def test_policy_boolean_is_not_a_truthy_string():
    async def check(store,*_):
        with pytest.raises(ValueError,match="bool"):
            await store.set_blocked("+84901234567","False")
    run(with_store(check))

@pytest.mark.parametrize("phone",["84901234567",84901234567,"+0123456789","not-a-phone"])
def test_canonical_phone_is_required_before_any_rpc(phone):
    class Client:
        async def eval(self,*args):raise AssertionError("invalid phone sent to Redis")
    async def check():
        with pytest.raises(ValueError,match="E.164"):
            await OnlineGate(PolicyStore(Client())).check_call_allowed(phone)
    run(check())
