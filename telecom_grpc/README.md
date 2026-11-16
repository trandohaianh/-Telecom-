# Telecom gRPC online policy edge

```text
SIP/IMS call-control -- gRPC/HTTP2 + mTLS --> CallPolicyService
                                               | Local immutable Bloom snapshot
                                               | Redis Lua version/size fence; exact member on maybe-hit
                                               +--> Redis deny set + manifest
CDR/Kafka -> analytics/streaming -> policy publisher -> Redis + Bloom refresh
```

The service reuses the version-fenced RAM Bloom and Redis Lua implementation in `ram_online/`. It retains an exact Redis consistency check so an out-of-date Bloom snapshot cannot cause an incorrect ALLOW. Thus Bloom negatives still use one small Redis Lua round trip in strict mode. Removing that round trip requires an atomic publication/fencing protocol and accepts bounded policy propagation delay. Redis timeout/outage opens a process-local circuit and applies fail-open (`allowed=true`) while reporting `UNKNOWN` on both gRPC APIs; the policy was not verified. Invalid phone input returns INVALID_ARGUMENT and is never fail-open.

The TelecomDecision.CheckCallAllowed protobuf API is added alongside the existing CallPolicy API. It accepts phone_number and optional imsi, and returns allowed, the policy manifest version, and ALLOW/BLOCK/UNKNOWN. The legacy RPC adds the UNKNOWN enum value (3), preserving existing field numbers and ALLOW/BLOCK values. TELECOM_BLOOM_FAST_ALLOW=1 enables immediate local ALLOW on a Bloom negative while the snapshot is fresh. Freshness is bounded by TELECOM_BLOOM_MAX_STALENESS_MS (default 10000); after expiry, requests return to the exact Redis Lua check. If Redis is also unavailable, the response is UNKNOWN with fail-open ALLOW. A newly blocked number can therefore be missed only inside the explicitly configured freshness window, even if snapshot refresh keeps failing. Keep fast mode disabled unless the operator and carrier explicitly accept that bounded propagation delay. A Bloom positive always goes through the atomic exact Redis check.

The client deadline is 30ms in the example; the service caps its Redis timeout at 10ms and defaults its online budget to 25ms. Fail-open means fraud protection was unavailable. Redis requests use a preconfigured async connection pool with retries disabled. Run multiple instances across zones and provision independent monitoring before carrier integration.

Build from repository root with `docker build -f telecom_grpc/Dockerfile .`. Protobuf Python modules are generated during image build. Supply Redis policy access, server certificate/key, and client CA through a secret manager. mTLS is enabled by default; insecure mode only accepts a loopback bind. The client example uses a client certificate because server-side client authentication is required.

Provision an empty policy only once with `python -m telecom_grpc.provision_policy`; never auto-initialize production state on startup. Restore manifest and exact set together from the policy source of truth. The Compose example supplies one persistent Redis node for integration, not Redis HA. Connect the existing Kafka/streaming pipeline's policy publisher to the same namespace.

For local integration, `.\bootstrap-local.ps1` creates disposable Redis credentials and local mTLS identities. Then run `docker compose up --build`; initialize the empty policy once with `docker compose run --rm --entrypoint python call-policy-grpc -m telecom_grpc.provision_policy`. The generated test client key is for local calls only and is not mounted into the server container.

The latency target is an engineering budget, not a result certified by source code. Validate p50/p95/p99/max and degraded rates through the SIP/IMS path under representative load, Redis failover, packet loss, CPU contention, and cold starts. Protobuf reduces serialization overhead; it cannot remove network RTT or guarantee <30ms.
