"""Open-loop HTTP benchmark for a dedicated shadow instance.

Legacy timing fields are retained. Only explicit scoring evidence can satisfy
the new scored-deadline metrics; quick pending/fail-open replies cannot.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import math
from numbers import Integral
from pathlib import Path
import sys
import time
import uuid

import httpx
import numpy as np

from security import sign_network, network_timestamp
from precise_event_loop import run_async

DEADLINE_MS = 30.0
ARRIVAL_PACING = "thread_sleep" if sys.platform == "win32" else "asyncio_sleep"
_MISSING = object()
_OUTCOMES = ("scored_before_deadline", "pending", "fail_open", "scored_after_deadline",
             "scored_deadline_unverified", "not_scored", "scoring_unreported",
             "scoring_inconsistent", "http_error", "invalid_response", "client_error",
             "client_shed")


def validate_run_parameters(*, rate, seconds, concurrency=32, timeout_s=2.0):
    """Validate before a client/server is started; return normalized run values."""
    try:
        numbers = tuple(float(value) for value in (rate, seconds, timeout_s))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("rate, seconds and timeout must be finite and positive") from error
    if any(isinstance(value, bool) for value in (rate, seconds, timeout_s)) or any(
            not math.isfinite(value) or value <= 0 for value in numbers):
        raise ValueError("rate, seconds and timeout must be finite and positive")
    if isinstance(concurrency, bool) or not isinstance(concurrency, Integral) or concurrency < 1:
        raise ValueError("concurrency must be a positive integer")
    rate, seconds, timeout_s = numbers
    offered = rate * seconds
    if not math.isfinite(offered) or offered < 1:
        raise ValueError("run must offer a finite number of at least one event")
    return rate, seconds, int(concurrency), timeout_s, int(offered)


def parse_rates(value):
    try:
        rates = tuple(float(item.strip()) for item in value.split(","))
    except (TypeError, AttributeError, ValueError) as error:
        raise ValueError("rates must be comma-separated finite positive numbers") from error
    if not rates or any(not math.isfinite(rate) or rate <= 0 for rate in rates):
        raise ValueError("rates must be comma-separated finite positive numbers")
    return rates


def validate_output_destination(path):
    """Reject existing output before any expensive benchmark side effects."""
    if path is not None:
        path = Path(path)
        if path.exists() or path.is_symlink():
            raise ValueError(f"output already exists: {path}")
        if not path.parent.is_dir():
            raise ValueError(f"output parent directory does not exist: {path.parent}")


def finite_latency(value):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0 else None)


def verdict_outcome(result):
    """Classify an HTTP-success verdict; deadline assertions require evidence."""
    if result.get("pending"):
        return "pending"
    if result.get("fail_open"):
        return "fail_open"
    scored = result.get("scored", _MISSING)
    deadline = result.get("scored_before_deadline", _MISSING)
    if scored is False and deadline is True:
        return "scoring_inconsistent"
    if deadline is not _MISSING and not isinstance(deadline, bool):
        return "scored_deadline_unverified" if scored is True else "scoring_unreported"
    if scored is True or deadline is True:
        latency = finite_latency(result.get("latency_ms"))
        if deadline is False:
            return "scored_after_deadline"
        if latency is None:
            return "scored_deadline_unverified"
        return "scored_before_deadline" if latency <= DEADLINE_MS else "scored_after_deadline"
    if scored is False or deadline is False:
        return "not_scored"
    return "scoring_unreported"


async def wait_until(arrival):
    """Pace arrivals without the coarse Windows IOCP timeout rounding.

    Python 3.11+ time.sleep uses a Windows waitable timer. Waiting on the one
    producer's executor thread wakes the loop through its completion port;
    it does not change system timer resolution or spin. The original scheduled
    arrival remains the latency origin even if the producer wakes late.
    """
    while (delay := arrival - time.perf_counter()) > 0:
        if ARRIVAL_PACING == "thread_sleep":
            await asyncio.to_thread(time.sleep, delay)
        else:
            await asyncio.sleep(delay)


async def run_http(url, key, *, rate, seconds=5.0, concurrency=32, timeout_s=2.0):
    rate, seconds, concurrency, timeout_s, count = validate_run_parameters(
        rate=rate, seconds=seconds, concurrency=concurrency, timeout_s=timeout_s)
    run_id = uuid.uuid4().hex[:12]
    status, decisions, ingest = Counter(), Counter(), Counter()
    outcomes = Counter({name: 0 for name in _OUTCOMES})
    latencies, queue_delays, roundtrips, server_latencies = [], [], [], []
    commit_latencies, server_queue_latencies, pending_tasks = [], [], set()
    shed, fail_open, pending_responses, scored_responses, unexpected_enforce = 0, 0, 0, 0, 0
    http_within, server_within, scored_http_within = 0, 0, 0
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=timeout_s, trust_env=False) as client:
        # Connection setup is outside the offered-arrival clock.
        try:
            await client.get(url.rstrip("/") + "/health/live")
        except httpx.HTTPError:
            pass
        start = time.perf_counter()
        epoch = time.time()

        async def send(index, arrival):
            nonlocal fail_open, pending_responses, scored_responses, unexpected_enforce
            nonlocal http_within, server_within, scored_http_within
            event = {
                "event_id": f"HTTPBENCH-{run_id}-{index}", "call_id": f"HTTPBENCH-{run_id}-{index}",
                "event_type": "CALL", "ts": epoch + index / rate,
                "caller": f"849{index % 1000:08d}", "callee": f"843{(index + 1) % 1000:08d}",
                "cell_id": "HN-0001", "imei": f"35391110{index % 1000:07d}",
                "origin_trunk": "LOCAL", "network": "4G", "caller_age_days": 500,
            }
            body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
            timestamp = network_timestamp()
            headers = {"content-type": "application/json", "x-timestamp": timestamp,
                       "x-signature": sign_network(body, timestamp, key)}
            request_start = time.perf_counter()
            queue_delays.append(max(0.0, (request_start - arrival) * 1000))
            received_response, outcome = False, None
            try:
                response = await client.post(url.rstrip("/") + "/api/v1/verdict", content=body, headers=headers)
                received_response = True
                status[str(response.status_code)] += 1
                if not response.is_success:
                    outcome = "http_error"
                else:
                    try:
                        result = response.json()
                        if not isinstance(result, dict):
                            raise ValueError("verdict must be a JSON object")
                    except ValueError:
                        outcome = "invalid_response"
                    else:
                        decision, admission = result.get("decision"), result.get("ingest")
                        decisions[decision if isinstance(decision, str) else "UNKNOWN"] += 1
                        ingest[admission if isinstance(admission, str) else "UNREPORTED"] += 1
                        latency = finite_latency(result.get("latency_ms"))
                        if latency is not None:
                            server_latencies.append(latency)
                            server_within += int(latency <= DEADLINE_MS)
                        commit = finite_latency(result.get("commit_ms"))
                        if commit is not None:
                            commit_latencies.append(commit)
                        queue = finite_latency(result.get("queue_ms"))
                        if queue is not None:
                            server_queue_latencies.append(queue)
                        fail_open += int(bool(result.get("fail_open")))
                        pending_responses += int(bool(result.get("pending")))
                        scored_responses += int(result.get("scored") is True or (
                            result.get("scored_before_deadline") is True and result.get("scored") is not False))
                        unexpected_enforce += int(result.get("mode") == "enforce")
                        outcome = verdict_outcome(result)
            except httpx.HTTPError:
                status["client_error"] += 1
                outcome = "client_error"
            finally:
                done = time.perf_counter()
                elapsed = (done - arrival) * 1000
                latencies.append(elapsed)
                roundtrips.append((done - request_start) * 1000)
                outcomes[outcome or "client_error"] += 1
                if received_response and elapsed <= DEADLINE_MS:
                    http_within += 1
                    scored_http_within += int(outcome == "scored_before_deadline")

        for index in range(count):
            arrival = start + index / rate
            await wait_until(arrival)
            pending_tasks = {task for task in pending_tasks if not task.done()}
            if len(pending_tasks) >= concurrency:
                shed += 1
                continue
            pending_tasks.add(asyncio.create_task(send(index, arrival)))
        if pending_tasks:
            await asyncio.gather(*pending_tasks)
    wall = time.perf_counter() - start
    outcomes["client_shed"] = shed

    def percentiles(values):
        return {f"p{p}": round(float(np.percentile(values, p)), 3)
                for p in (50, 95, 99)} if values else {}

    def pct(value):
        return round(100 * value / count, 3)

    legacy_within = sum(value <= DEADLINE_MS for value in latencies)
    return {
        # Existing keys are retained for comparisons with earlier reports.
        "kind": "http_end_to_end", "offered_rate_eps": rate, "offered_events": count,
        "arrival_pacing": ARRIVAL_PACING,
        "completed_requests": len(latencies), "client_shed": shed, "wall_s": round(wall, 3),
        "completed_eps": round(len(latencies) / wall, 2), "http_status": dict(status),
        "decisions": dict(decisions), "ingest": dict(ingest), "fail_open": fail_open,
        "unexpected_enforce": unexpected_enforce, "latency_ms": percentiles(latencies),
        "scheduler_delay_ms": percentiles(queue_delays),
        "request_roundtrip_ms": percentiles(roundtrips), "server_reported_latency_ms": percentiles(server_latencies),
        "responses_within_30ms_pct": pct(legacy_within),
        "note": "all offered requests are denominator; legacy response timing is not proof of model scoring",
        # Deadline success excludes pending/fail-open/errors/shedding/unreported scoring.
        "deadline_ms": DEADLINE_MS, "denominator": "offered_events", "concurrency": concurrency,
        "offered_window_s": seconds, "timeout_s": timeout_s, "response_outcomes": dict(outcomes),
        "scored_responses": scored_responses, "pending_responses": pending_responses,
        "pending_responses_pct": pct(pending_responses), "fail_open_pct": pct(fail_open),
        "scoring_unreported": outcomes["scoring_unreported"],
        "scored_before_deadline": outcomes["scored_before_deadline"],
        "scored_before_deadline_pct": pct(outcomes["scored_before_deadline"]),
        "scored_within_http_30ms": scored_http_within, "scored_within_http_30ms_pct": pct(scored_http_within),
        "http_responses_within_30ms": http_within, "http_responses_within_30ms_pct": pct(http_within),
        "server_responses_within_30ms": server_within, "server_responses_within_30ms_pct": pct(server_within),
        "server_commit_ms": percentiles(commit_latencies), "server_queue_ms": percentiles(server_queue_latencies),
        "metric_definitions": {
            "scored_before_deadline": "2xx verdict; explicit scored deadline flag preferred, otherwise scored=true; "
                "not pending/fail_open; finite server latency <=30ms",
            "scored_within_http_30ms": "scored_before_deadline plus arrival-to-completed-HTTP-response <=30ms",
            "responses_within_30ms_pct": "legacy timing of completed attempts, including quick errors; not scored success",
            "response_outcomes": "mutually exclusive categories summing to offered_events; pending/fail_open counters overlap",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="URL of a dedicated shadow benchmark instance")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--rates", default="100,500,1000")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_output_destination(args.out)
        rates = parse_rates(args.rates)
        for rate in rates:
            validate_run_parameters(rate=rate, seconds=args.seconds, concurrency=args.concurrency)
        key = bytes.fromhex(args.key_file.read_text(encoding="utf-8").strip())
        if len(key) != 32:
            raise ValueError("network key must contain 32 bytes encoded as hex")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    reports = [run_async(run_http(args.url, key, rate=rate, seconds=args.seconds,
                                    concurrency=args.concurrency)) for rate in rates]
    content = json.dumps({"runs": reports}, indent=2)
    if args.out:
        with args.out.open("x", encoding="utf-8") as destination:
            destination.write(content + "\n")
    print(content)


if __name__ == "__main__":
    main()
