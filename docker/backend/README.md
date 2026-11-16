# Isolated Linux backend verification

This image runs the existing HTTP smoke, restart and load verifier on container loopback. Source, static assets and model weights are mounted read-only; no host port or external network is used. Credentials, SQLite state and evidence belong only to the disposable test.

From the repository root in PowerShell:

```powershell
docker build --pull=false --tag telecom-backend-linux:local docker/backend
$testSuffix = Get-Date -Format yyyyMMddHHmmss
$testVolume = "telecom-backend-$testSuffix"
$testContainer = "telecom-backend-http-$testSuffix"
$projectPath = (Get-Location).Path
docker volume create $testVolume
docker run --rm --network none --read-only --user 0:0 --entrypoint python --mount "type=volume,source=$testVolume,target=/tmp" telecom-backend-linux:local -B -c "import os; os.chmod('/tmp',0o1777)"
docker run --rm --name $testContainer --network none --read-only --cpus 2 --memory 2g --mount "type=volume,source=$testVolume,target=/tmp" --mount "type=bind,source=$projectPath/src,target=/workspace/src,readonly" --mount "type=bind,source=$projectPath/static,target=/workspace/static,readonly" --mount "type=bind,source=$projectPath/runtime/artifacts_v22/models,target=/artifacts/models,readonly" telecom-backend-linux:local --rates 10,50,100 --seconds 10 | Set-Content -LiteralPath "data/http_linux_$testSuffix.json" -Encoding UTF8
docker volume rm $testVolume
```

Check the verifier command succeeds before removing its volume. On failure, keep the volume for diagnosis and inspect only the container/volume created by this test. Standard error contains disposable-server diagnostics; the entrypoint never prints credentials or signing keys.

Use a new named volume backed by disk for /tmp. Do not substitute tmpfs for a durability/capacity measurement: SQLite FULL on RAM omits storage costs. Docker Desktop still adds virtualization and host caching; this is not a power-loss test.

The report distinguishes offered/completed/client-shed requests, fail-open and overload, server latency, HTTP roundtrip and scheduled end-to-end latency. HTTP 200/fail-open does not establish successful inference within the deadline. Each rate runs for three seconds by default; these short observations do not certify production capacity, high availability or fraud accuracy.

Runtime settings remain SQLite WAL/FULL, one inference owner, a 30 ms CALL budget and shadow mode. The image records Python/package versions, cgroup limits and source hashes. The saved v2.2 run additionally records the disk-backed ext4 mount and image tag.

Dependency pins match the Windows reference environment to aid reproduction; Python is 3.11 in this Linux image and 3.12 on the reference host. During the reference build, pip marked NumPy 2.4.0 as yanked. These pins describe that benchmark environment, rather than a production dependency recommendation or a complete transitive lock.

See [the v2.2 validation report](../../docs/VALIDATION_V22.md) for measured outcomes and remaining limitations.

The final 2026-10-22 measurements and retry/durability contract are in [SLA_30MS.md](../../docs/SLA_30MS.md). The example offers each rate for ten seconds; entrypoint defaults remain three seconds. Scored success uses all offered events as denominator and requires a post-commit result; pending/fail-open/shedding are failures. Raw latency percentiles cover completed attempts. Neither environment met the end-to-end 30 ms SLA.

The [2026-10-25 source verification](../../data/http_bugfix_linux_v22.json)
passed all eight HTTP/restart smoke checks and evidence validation on the final
source, with no unexpected enforce. It used a dedicated disk volume, two CPUs,
2 GiB and five seconds each at 10/50 EPS. This is a functional regression check,
not a capacity certificate; scheduled HTTP p99 was 52.078/51.512 ms. The owned
container and volume were removed. Source hashes, image ID and mount evidence
are included in the report. See [bug fixes and limits](../../docs/BUGFIX_20261025.md).

The [latest source check](../../data/http_sla_target_linux_final_20261025.json)
also passed all eight smoke checks and evidence validation; its p99 remained
39.083/41.722 ms at 10/50 EPS. Owned resources were removed. The
[SLA optimization report](../../docs/SLA_TARGET_20261025.md) records the retained
WAL/FULL configuration, all unsuccessful capacity runs and final source hashes.
