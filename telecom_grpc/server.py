"""Async gRPC edge for the existing fail-open, version-guarded policy gate."""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from contextlib import suppress
from urllib.parse import quote, urlsplit, urlunsplit

import grpc
from ram_online.core import OnlineGate, OnlineUnavailable, PolicyStore, make_client, snapshot_is_fresh
from ram_online.production import FailOpenGate
from redis.exceptions import RedisError

from . import telecom_pb2, telecom_pb2_grpc
from .tls_config import server_credentials


def policy_url() -> str:
    url = os.getenv("POLICY_REDIS_URL", "redis://redis-policy:6379/0")
    password_file = os.getenv("POLICY_REDIS_PASSWORD_FILE")
    if password_file:
        parts = urlsplit(url)
        if parts.username or parts.password:
            raise RuntimeError("use either URL credentials or POLICY_REDIS_PASSWORD_FILE")
        netloc = ":" + quote(open(password_file, encoding="utf-8").read().strip(), safe="") + "@" + parts.netloc
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


class CallPolicyService(telecom_pb2_grpc.CallPolicyServicer):
    def __init__(self, gate: FailOpenGate):
        self.gate = gate

    async def CheckCallAllowed(self, request, context):
        started = time.perf_counter()
        if not request.phone_number:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "phone_number is required")
        try:
            decision = await self.gate.check_call_allowed(request.phone_number)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        status = (telecom_pb2.ALLOW if decision.allowed else telecom_pb2.BLOCK)
        if decision.policy_status != "known":
            status = telecom_pb2.UNKNOWN
        return telecom_pb2.CheckCallReply(
            decision=status,
            policy_status=decision.policy_status,
            reason=decision.reason,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def Health(self, request, context):
        return telecom_pb2.HealthReply(status="serving", circuit=self.gate.breaker.state)


class TelecomDecisionService(telecom_pb2_grpc.TelecomDecisionServicer):
    """Compatibility API with explicit UNKNOWN/fail-open and policy version."""
    def __init__(self, gate: FailOpenGate, bloom_max_staleness_ms: float | None = None):
        self.gate = gate
        if bloom_max_staleness_ms is None:
            try:
                bloom_max_staleness_ms = float(os.getenv("TELECOM_BLOOM_MAX_STALENESS_MS", "10000"))
            except ValueError as exc:
                raise RuntimeError("TELECOM_BLOOM_MAX_STALENESS_MS must be numeric") from exc
        if not math.isfinite(bloom_max_staleness_ms) or bloom_max_staleness_ms <= 0:
            raise RuntimeError("TELECOM_BLOOM_MAX_STALENESS_MS must be positive and finite")
        self.bloom_max_staleness_ms = bloom_max_staleness_ms

    async def CheckCallAllowed(self, request, context):
        if not request.phone_number:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "phone_number is required")
        try:
            # Optional low-latency mode trades immediate blacklist propagation for
            # a local Bloom-negative ALLOW. Keep disabled until the operator has
            # accepted and bounded snapshot propagation delay.
            snapshot = self.gate.gate.snapshot
            if (os.getenv("TELECOM_BLOOM_FAST_ALLOW", "0") == "1"
                    and snapshot is not None
                    and snapshot_is_fresh(snapshot, self.bloom_max_staleness_ms)
                    and re.fullmatch(r"\+[1-9][0-9]{7,14}", request.phone_number)
                    and request.phone_number not in snapshot.bloom):
                return telecom_pb2.CallResponse(
                    allowed=True,
                    policy_version=snapshot.manifest,
                    status=telecom_pb2.CallResponse.ALLOW,
                )
            decision = await self.gate.check_call_allowed(request.phone_number)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        # Exact Redis decisions carry the manifest read atomically with membership.
        # Fail-open UNKNOWN deliberately has no claimed policy version.
        version = decision.policy_version
        if decision.policy_status != "known":
            status = telecom_pb2.CallResponse.UNKNOWN
        else:
            status = (telecom_pb2.CallResponse.ALLOW if decision.allowed
                      else telecom_pb2.CallResponse.BLOCK)
        return telecom_pb2.CallResponse(
            allowed=decision.allowed,
            policy_version=version,
            status=status,
        )


async def serve() -> None:
    redis_url = policy_url()
    bind = os.getenv("GRPC_BIND", "0.0.0.0:50051")
    timeout_ms = float(os.getenv("REDIS_SOCKET_TIMEOUT_MS", "10"))
    if not 0 < timeout_ms <= 10:
        raise RuntimeError("REDIS_SOCKET_TIMEOUT_MS must be in (0, 10]")
    redis_client = make_client(redis_url, max_connections=int(os.getenv("REDIS_POOL_SIZE", "256")), io_timeout=timeout_ms / 1000)
    builder = make_client(redis_url, max_connections=2, io_timeout=1.0)
    gate = OnlineGate(PolicyStore(redis_client, os.getenv("POLICY_NAMESPACE", "telecom-policy")),
                      budget_ms=float(os.getenv("ONLINE_BUDGET_MS", "25")),
                      max_inflight=int(os.getenv("MAX_INFLIGHT", "1024")))
    fail_open = FailOpenGate(gate)

    async def refresh():
        while True:
            try:
                async with asyncio.timeout(30):
                    await gate.refresh_snapshot(builder)
            except (RedisError, OnlineUnavailable, TimeoutError, ValueError):
                # Keep last snapshot. Strict lookups remain Lua-fenced; optional local
                # ALLOW expires after TELECOM_BLOOM_MAX_STALENESS_MS.
                pass
            await asyncio.sleep(float(os.getenv("BLOOM_REFRESH_SECONDS", "10")))

    refresh_task = asyncio.create_task(refresh(), name="bloom-refresh")
    server = grpc.aio.server(options=[
        ("grpc.so_reuseport", 1), ("grpc.max_concurrent_rpcs", int(os.getenv("MAX_GRPC_RPCS", "2048"))),
        ("grpc.keepalive_time_ms", 30000), ("grpc.keepalive_timeout_ms", 5000),
        ("grpc.http2.max_pings_without_data", 0),
    ])
    telecom_pb2_grpc.add_CallPolicyServicer_to_server(CallPolicyService(fail_open), server)
    telecom_pb2_grpc.add_TelecomDecisionServicer_to_server(TelecomDecisionService(fail_open), server)
    # mTLS is required unless an explicitly local-only insecure listener is selected.
    if os.getenv("GRPC_INSECURE_LOCAL", "0") == "1":
        if bind not in {"127.0.0.1:50051", "localhost:50051"}:
            raise RuntimeError("insecure gRPC must bind loopback only")
        server.add_insecure_port(bind)
    else:
        server.add_secure_port(bind, server_credentials())
    await server.start()
    try:
        await server.wait_for_termination()
    finally:
        await server.stop(grace=2)
        refresh_task.cancel()
        with suppress(asyncio.CancelledError):
            await refresh_task
        await redis_client.aclose()
        await builder.aclose()


def main():
    asyncio.run(serve())


if __name__ == "__main__":
    main()
