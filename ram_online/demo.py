"""Executable simulation. Provision only an explicitly isolated demo namespace."""
import argparse
import asyncio
import json
import statistics
import time
import uuid

from .core import OnlineGate, OnlineUnavailable, PolicyStore, make_client, run
from .streaming import SlidingWindowDetector


async def main(args):
    policy_client = make_client(args.policy_redis, io_timeout=1.0)
    lookup_client = make_client(args.policy_redis)
    analytics_client = make_client(args.analytics_redis, io_timeout=1.0)
    namespace = "demo-" + uuid.uuid4().hex
    store = PolicyStore(policy_client, namespace)
    gate = OnlineGate(PolicyStore(lookup_client, namespace), capacity=1000, budget_ms=args.budget_ms)
    caller = "+84901234567"
    try:
        await store.initialize_empty_policy()
        await gate.refresh_snapshot(policy_client)
        await lookup_client.ping()
        assert await gate.check_call_allowed(caller) is True
        detector = SlidingWindowDetector(analytics_client, store, threshold=3,
            namespace=namespace, activate_demo_blocks=True)
        for index in range(4):
            result = await detector.process({"caller": caller, "call_id": "call-" + str(index),
                "setup_ts_ms": time.time_ns() // 1_000_000})
        # Gate still has the OLD Bloom: version mismatch must find the new deny.
        assert result["flagged"]
        assert await gate.check_call_allowed(caller) is False
        await gate.refresh_snapshot(policy_client)
        assert await gate.check_call_allowed("+84909999999") is True
        durations, good, degraded, incorrect = [], 0, 0, 0
        for index in range(args.samples):
            phone = caller if index % 10 == 0 else "+84909999999"
            started = time.perf_counter()
            try:
                allowed = await gate.check_call_allowed(phone)
            except OnlineUnavailable:
                allowed = None
            elapsed = (time.perf_counter() - started) * 1000
            durations.append(elapsed)
            if allowed is None or elapsed > args.budget_ms:
                degraded += 1
            elif allowed == (phone != caller):
                good += 1
            else:
                incorrect += 1
        ordered = sorted(durations)
        report = {"namespace": namespace, "samples": args.samples,
            "correct_within_budget": good, "degraded": degraded, "incorrect": incorrect,
            "lookup_budget_ms": args.budget_ms,
            "all_attempt_latency_ms": {"p50": statistics.median(ordered),
                "p99": ordered[min(len(ordered)-1, int(.99 * len(ordered)))], "max": max(ordered)},
            "scope": "sequential in-process function + real Redis; no HTTP/SIP load or capacity certification",
            "bloom_bytes": len(gate.snapshot.bloom.bits), "bloom_hashes": gate.snapshot.bloom.k}
        print(json.dumps(report, indent=2))
        if args.out:
            with open(args.out, "x", encoding="utf-8") as target:
                json.dump(report, target, indent=2)
    finally:
        # Only keys in this unique demonstration namespace.
        await policy_client.delete(store.manifest_key, store.deny_key)
        await analytics_client.delete("{" + namespace + ":" + caller + "}:calls",
                                      "{" + namespace + ":" + caller + "}:frontier")
        await policy_client.aclose()
        await analytics_client.aclose()
        await lookup_client.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-redis", required=True)
    parser.add_argument("--analytics-redis", required=True)
    parser.add_argument("--budget-ms", type=float, default=5.0)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.samples <= 0:
        parser.error("--samples must be positive")
    run(main(args))
