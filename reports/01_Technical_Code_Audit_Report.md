# Technical Code Audit & Refactoring Report

**Audit date:** 2026-11-13  
**Workspace:** D:/Du_An_1_Telecom_AI_Full/v2  
**Evidence basis:** Source and configuration inspection, project validation documents, and commands listed in §11.

## 1. Executive Summary

This workspace contains multiple deployable systems, not one application: the main Telecom Fraud Shield under src/, static/, models/, data/; a standalone RAM/Redis lookup prototype under ram_online/; a gRPC policy edge under telecom_grpc/; Spark/Kafka feature pipelines under spark/; Edu DSS under edu_dss/; and an Android WebView client under android/. backups/, backup_tfd-v2.0/, generated artifacts, benchmark outputs, and docs/build/node_modules/ are historical or generated material, not active application modules. They were preserved.

The main fraud API has a connected FastAPI request path, detector, feature state, bounded inference runtime, SQLite/WAL journal, evidence log, operator authentication and signed network endpoints. Its training and evaluation evidence is mainly generated from simulated traffic. The project documentation explicitly says the HTTP p99 target of 30 ms and 50,000 TPS target have not been met, live network mode defaults to shadow, and no real labeled subscriber corpus certifies the advertised metrics.

One concrete security mismatch was fixed. telecom_grpc/server.py previously allowed a secure listener to run without client certificate verification if GRPC_CLIENT_CA was unset. That contradicted the README and Compose mTLS contract. Credential construction now lives in telecom_grpc/tls_config.py; the secure listener fails closed without a client CA and always requires client authentication. Two regression tests cover this behavior.

**Production assessment:** not production-ready for carrier decisions or institutional student decisions. Demo and service paths exist, but representative real data, external adapters, carrier integration, HA/restore exercises and target-load evidence remain absent or unverified.

## 2. Project Structure

| Path | Observed role | Status and limits |
|---|---|---|
| src/ | Main fraud simulator, feature extraction, training, inference, API, persistence and security | Primary application source. run_demo.py imports server.app; run_live.py is the live launcher. |
| static/ | Dashboard and subscriber PWA | Browser assets are served by the main API. |
| models/, data/ | Model binaries, thresholds, datasets and evaluation artifacts | Main app defaults. Much evidence is synthetic or generated. |
| runtime/ | Rebuilt bundles, verification outputs and runtime dependencies | Contains generated snapshots. run_demo.py may select runtime/artifacts_v22 when both manifests exist. |
| tests/ | Main fraud-system regression tests | Active suite: 405 passed in §11. |
| ram_online/ | Bloom plus exact Redis policy lookup and Kafka prototype | Separate system; its README says it is not migrated into the main live API. |
| telecom_grpc/ | Async carrier policy gRPC edge over Redis gate | Separate deployment requiring Redis and certificates. |
| spark/ | Batch/distributed/stateful CALL feature calculation and Kafka stream | Separate compute path; local parity evidence exists, HA and production capacity do not. |
| edu_dss/ | Streamlit UI, FastAPI backend, Celery/Redis jobs and demo fixtures | Independent application; production domain adapters and identity integration are required. |
| android/ | Kotlin Android WebView wrapper | Demo client; no native call screening. |
| docs/ | Validation, runbooks, generated reports and figures | Historical snapshots must be read with their stated date/version. |
| backups/, backup_tfd-v2.0/ | Historical source, tests and models | Not active runtime paths; root-wide pytest incorrectly collects some old tests. |
| docs/build/node_modules/ | Bundled JavaScript dependencies | Generated third-party code; not audited file-by-file. |

No root Git metadata was present: git status returned “not a git repository”. Therefore Git provenance and the pre-existing diff state could not be verified.

## 3. Current Implementation Status

| Capability | Status | Evidence and limit |
|---|---|---|
| Main FastAPI dashboard/API | Implemented for demo/local operation | run_demo.py:main; src/server.py:lifespan and routes for /api/v1/verdict, /api/v1/cdr, /api/login and /api/app/*. |
| Carrier verdict link | Signed JSON API implemented; carrier adapter absent | src/server.py:network_request, network_verdict, network_cdr; HMAC/timestamp verification in src/security.py:verify_network. Needs operator integration and live testing. |
| CALL feature pipeline | Implemented in causal main FeatureStore | src/feature_store.py:FeatureStore.call_features maintains windows, SIM/IMEI links, mobility and CDR-derived history. |
| Tier-1/2 models | Training and runtime scoring implemented | src/train.py:train_call, train_sms, train_audio; src/detector.py:Detector. Evidence uses generated datasets and does not prove real-traffic performance. |
| Voice WAV analysis | Bounded PCM and acoustic feature path implemented | src/voice_pipeline.py:decode_wav and analyze_wav; local ASR is optional. docs/VALIDATION_V22.md says there is no certified real WAV corpus/model. |
| Fail-open and circuit breaker | Implemented | src/resilience.py:CircuitBreaker, FallbackRules, GuardedDetector; operational fail-open must be distinguished from successful scoring. |
| Durable inference state | Implemented for one owner using SQLite/WAL | src/backend_state.py:BackendState and src/backend_runtime.py:InferenceRuntime. Not a distributed HA store. |
| Evidence and operator audit | Local implementation exists | src/evidence.py, src/backend_evidence.py, src/security.py:AuditLog and verify_audit. Key custody/external anchoring remain risks. |
| Agent | Advisory flow implemented; human approval required | src/agent.py:OpsAgent; admin decision route in src/server.py. No direct agent network mutation path was found. |
| Subscriber PWA | Demo/API-backed paths present | /api/app/* in src/server.py and static assets mounted under /app. Demo sessions/in-memory structures are not real OTP/customer storage. |
| Android | WebView shell implemented | android/app/src/main/java/vn/lachan/fraudshield/MainActivity.kt; no native call screening per README. |
| RAM deny lookup | Standalone prototype implemented | ram_online/core.py, streaming.py, production.py. Strict mode requires Redis; outage produces UNKNOWN/fail-open. Not connected to main API. |
| gRPC edge | Separate service implemented | telecom_grpc/server.py. Needs Redis policy and TLS assets; no carrier connection was verified. mTLS mismatch fixed here. |
| Spark/Kafka | Distributed feature paths implemented | spark/README.md and stateful_stream.py; parity/restart checks are documented, but storage is snapshot-based and multi-host HA is not established. |
| Edu DSS | UI/API/queue path implemented; domain logic adapter/demo dependent | edu_dss/backend/app/main.py, services.py and grade_baseline.py. Fixtures are marked demo_fixture_only; graph adapter returns adapter_required. |

## 4. System Architecture

Main fraud request flow:

    Simulator / file / signed carrier copy
       -> FastAPI routes (src/server.py)
       -> validation and InferenceRuntime bounded queue/owner
       -> IngestGuard and causal FeatureStore
       -> Detector (CALL/SMS/AUDIO models)
       -> GuardedDetector (deadline, breaker, fallback)
       -> SQLite transaction/journal and evidence publication
       -> JSON response and dashboard/subscriber PWA

Startup is run_demo.py:main -> model/data selection -> import server.app -> Uvicorn with the custom event loop. src/server.py:lifespan creates LiveEngine, detector, state and evidence components. The network mode initializes to shadow on engine construction. SQLite/WAL is the main local durable state; JSONL evidence/audit logs are separate append-only artifacts.

Separate deployment flow:

    Carrier adapter -> telecom_grpc -> local Bloom snapshot + exact Redis check
    Kafka copies/CDRs -> ram_online or Spark stream -> policy/snapshot publisher
    Browser -> Edu DSS Streamlit -> FastAPI/JWT -> Celery -> Redis

There is no observed src/server.py route delegating inference to ram_online or gRPC. Edu DSS owns a separate backend. Android loads the subscriber web app and is not an OS call-control service.

## 5. Module-by-Module Analysis

### Main src/

| Modules | Responsibility and findings |
|---|---|
| server.py, config.py, run_demo.py, run_live.py | Entrypoints, configuration, API routes, lifecycle and UI wiring. Demo mode sets FRAUD_DEMO=1 and can print demo tokens. Live deployment requires operator configuration. |
| backend_runtime.py, backend_state.py, backend_evidence.py | Bounded inference owner, SQLite transactions/journal/checkpoint recovery and durable evidence acknowledgements. Robust local design, but single-owner SQLite is not horizontal HA. |
| feature_store.py, ingest_guard.py, events_io.py, data_generator.py | Causal feature state, duplicate/lateness control, loading and synthetic generation. Simulator assumptions influence learned decision boundaries. |
| train.py, detector.py, fast_iforest.py, model_manifest.py, textnorm.py, reason_codes.py | Training, runtime scoring, artifact validation and explanation codes. Some algorithms are library-provided; custom fast scoring requires parity checks. |
| resilience.py, precise_event_loop.py | Deadlines, fallback, breaker and loop timing. Fail-open means policy confidence is unavailable. |
| security.py, agent.py, evidence.py | Roles, sessions, signed request HMAC, hash-chained audit and proposal workflow. Local key custody remains a production concern. |
| voice_api.py, voice_pipeline.py, voice_model.py, train_voice.py | WAV validation, PCM decode/resampling, energy VAD, acoustic features, optional local ASR and a separate calibrated model bundle. No real annotated WAV certification. |
| evaluate.py, statistical_reporting.py, drift.py, shadow_run.py, shadow_feedback.py, experiment_guard.py | Offline metrics, confidence intervals, PSI drift reporting and shadow experiments. Simulation evidence is not deployment certification. |
| loadtest.py, http_loadtest.py, verify_live_http.py, rebuild_artifacts.py | Test/rebuild tools, not request-serving modules. Rebuild tools can write model/data outputs and should use controlled output paths. |

### Adjacent applications

- ram_online/core.py implements Bloom membership and exact Redis Lua/version fencing; streaming.py handles Kafka; production.py wraps outage policy. README correctly notes strict mode still performs one Redis RPC, Kafka+Redis is not exactly-once, and stale Bloom behavior needs an explicit contract.
- telecom_grpc/server.py maps the Redis OnlineGate to gRPC; telecom.proto is compiled in Docker. Compose configures GRPC_CLIENT_CA. The secure-listener gap is fixed here. Compose Redis is one node, not HA.
- spark/spark_features.py selects batch/stream engines; stateful_stream.py and distributed_stream.py persist local/distributed key state. spark/VALIDATION.md documents parity/restart tests; full snapshots and shared filesystem are limits.
- edu_dss/backend/app/main.py protects role-specific routes and queues Celery tasks. services.py implements fixture or HTTP adapter logic; grade_baseline.py is synthetic. Streamlit is a separate API client. Production identity, adapters, tenant audit and Redis HA are outstanding.
- android/ is a WebView client around the PWA, not a call-screening implementation.

## 6. Technology Stack

| Technology | Observed version/config | Purpose and constraint |
|---|---|---|
| Python | Audit interpreter 3.12.4 | Main services and pipelines; documented projects require Python >=3.11. |
| FastAPI/Uvicorn | Root: FastAPI >=0.110, Uvicorn >=0.47; Edu pins 0.136.3/0.47.0 | ASGI APIs. Root ranges are not a full lock. |
| NumPy/pandas/SciPy/PyArrow | Root lower bounds: 1.26/2.1/1.11/14 | Numerical and tabular processing, Parquet data. |
| LightGBM | >=4.3 | Boosted tabular classifier; generalization depends on representative data. |
| scikit-learn | ==1.8.0 | Isolation Forest, TF-IDF, Logistic Regression; pin protects persisted model compatibility. |
| Numba | >=0.59 optional | Fast Isolation Forest traversal with NumPy fallback. |
| SQLite | WAL and synchronous FULL in backend_state.py | Local transactional journal; not a distributed HA database. |
| Redis/Kafka/gRPC | RAM requirements: redis 8.1.0, confluent-kafka 2.16.0; gRPC edge: grpcio/grpcio-tools 1.85.0 | Separate policy and streaming deployment; needs provisioned services, auth, capacity and failover tests. |
| Spark | Container/build-defined; installed runtime not verified | Distributed batch/stateful features; see spark docs. |
| Edu DSS | FastAPI 0.136.3, Celery 5.6.3, Redis 6.4.0, PyJWT 2.15.1, Pydantic 2.12.5; Streamlit 1.66.1 | Separate asynchronous job application; fixture adapters are not institutional models. |
| Android | SDK 34, min SDK 26, Java/JVM 17; AndroidX dependencies in app/build.gradle.kts | WebView wrapper; not native call control. |

Rationales visible in project code/docs: SQLite provides local atomic durability; Redis Lua fences version and exact membership atomically; Bloom reduces exact set work but not strict-mode network round trips; Spark partitions feature state by entity; FastAPI/Streamlit/Celery split API, UI and background work. Beyond these, initial technology selection cannot be proven from code alone.

## 7. Algorithms & Mathematical Foundations

Only algorithms visible in code are described; no formula is attributed to opaque artifacts without an implementation.

### Fraud models

- LightGBM binary classifier (src/train.py:train_call/train_audio; src/detector.py): additive decision trees optimize a binary loss. Fixed feature vectors yield scores used against stored BLOCK/FLAG thresholds. Training work depends on rows, features, boosting rounds and tree construction; memory is dominated by input matrices and tree nodes. It captures nonlinear tabular interactions, but scores are not automatically calibrated and synthetic distribution shift is material.
- Isolation Forest (train.py and fast_iforest.py): random partitions isolate unusual rows; shorter paths imply higher anomaly. Models are segmented and trained on legitimate examples. Approximate forest training cost is trees × subsample × log(subsample); score cost is trees × tree depth per row. Rarity is not equivalent to fraud.
- TF-IDF + Logistic Regression (train.py:sms_matrix/train_sms; runtime in detector.py): normalize/tokenize, create unigram/bigram sparse features with IDF and L2 normalization, then z=w·x+b and p=1/(1+exp(-z)). Sparse inference cost is proportional to active terms. Vocabulary drift and synthetic texts constrain results.
- Threshold selection (train.py:threshold_for_fpr and statistical_reporting.py): select empirical quantiles of negative calibration scores and apply floors. Zero observed false positives is not zero population risk; confidence limits and sample independence matter.

### Streaming features and drift

- Sliding windows (feature_store.py:FeatureStore.call_features): prune old deque entries, update counts/distinct callees/SIM-device associations and completed-duration history. Insertions are amortized near O(1) plus expired entries; association pruning can depend on active key count. Event order and recovery are correctness-critical.
- Haversine distance (data_generator.py:_haversine and feature derivation): with angular latitude/longitude, a=sin²(Δφ/2)+cosφ1 cosφ2 sin²(Δλ/2), distance=2R asin(sqrt(a)). At zero coordinate difference, a=0 and distance=0 km. One degree longitude at the equator is about 111.2 km for R=6371 km. Speed divides distance by a time interval floored at one minute and is capped at 5000 km/h. It is a simulated mobility proxy, not precise radio positioning.
- PSI (drift.py:psi and _psi_from_spec): PSI=Σᵢ(Aᵢ-Eᵢ)ln(Aᵢ/Eᵢ), where E and A are expected/reference and actual/current bin shares. Example E=(0.5,0.5), A=(0.8,0.2): 0.3 ln(1.6) + (-0.3) ln(0.4) ≈ 0.1409+0.2749=0.4159. The implementation creates reference quantile bins and smooths zero shares with EPS. PSI flags distribution shift; it cannot identify the cause or prove model degradation.
- WAV descriptors (voice_pipeline.py:_channel_metrics): RMS energy, zero-crossing rate, spectral flatness/centroid, spectral drift and autocorrelation-derived pitch proxy. They are signal descriptors/quality checks, not proof of deepfake. Cost scales with samples and frames; parser caps payload at 12 MiB and 30 seconds.
- Bloom sizing (ram_online/core.py; documented in ram_online/README): m≈-n ln(p)/(ln 2)^2 bits, k≈(m/n)ln 2. For n=1,000,000 and p=10^-4, m≈19.17 million bits (2.29 MiB) and k≈13. A negative is definitive only relative to that snapshot; a positive requires exact lookup. Redis version fencing prevents stale-snapshot false negatives in strict mode if Redis is authoritative.
- OLS grade fixture (edu_dss/backend/app/grade_baseline.py:_fit_ols): slope β1=Σ(x-x̄)(y-ȳ)/Σ(x-x̄)^2; intercept β0=ȳ-β1x̄; prediction β0+β1x. One feature and seven synthetic pairs make it a teaching fixture, not an academic decision model. Holdout uses last two rows for illustrative metrics; final demo predictor is fit on all seven.

## 8. Code Quality Issues

| Priority | Evidence and issue | Impact | Suggested response |
|---|---|---|---|
| Critical | Simulator and generated datasets are primary training/evaluation source (data_generator.py, build_dataset.py); docs disclaim real subscribers. | Simulated metrics do not establish carrier performance. | Collect governed representative data; use group/time isolation, independent calibration, adjudicated outcomes and shadow validation. |
| High | HTTP p99 30 ms and 50k TPS target are not met (docs/SLA_30MS.md, docs/VALIDATION_V22.md). | No signaling SLA/capacity claim is justified. | Reproduce on target hardware/network and report scored, pending, fail-open, shed and latency phases separately. |
| High | Main durable state is one SQLite owner; Redis Compose examples are single node. | HA and horizontal scale are not demonstrated. | Capacity plan, tested backups/restore, retention/migrations and HA design before go-live. |
| High | gRPC Redis failure returns UNKNOWN with fail-open allowed behavior (telecom_grpc README/server). | Downstream may mislabel unknown policy as verified allow. | Preserve UNKNOWN and expose degraded counters; require explicit carrier policy. |
| High | Root requirements are mostly lower bounds. | Environments can resolve different dependency/model combinations. | Lock dependencies per deployable service, publish SBOM and validate model compatibility. |
| Medium | Detector.process/ActionLedger is a separate path; main server uses InferenceRuntime orchestration. | Similar policy paths may drift; not proven dead. | Retain until callers/entrypoints are proven absent; document intended scope. |
| Medium | Root pytest discovery includes backup tests with missing import roots. | Generic root command fails for reasons unrelated to active app. | Configure active test roots and run each independent service in its own environment. |
| Medium | Edu DSS demo responses use fixtures and synthetic GPA baseline. | Demo output can be mistaken for institutional recommendation. | Preserve demo labels and require adapters/identity claims outside demo mode. |
| Medium | Android uses a WebView and emulator HTTP URL. | No native call interception; release transport not proven. | Separate release endpoint/configuration, require HTTPS and plan carrier/OS integration. |

## 9. Duplicate & Dead Code Analysis

- Historical backups, old model generations, generated docs dependencies, runtime verification trees and evidence outputs are present. They were not removed because they have rollback/reproducibility value.
- src/feature_store.py, ram_online/ and spark/ contain related feature/policy concepts but serve different state and deployment contracts. No safe merge is justified without migration and parity proof.
- ActionLedger is called by Detector.process, but this is not the main API inference path. A direct main-server caller was not found; static absence is insufficient to delete it because it is a public helper and may be used by clients.
- No broad unused-symbol or file deletion was performed. CLI, deployment and dynamic imports make name-only cleanup unsafe.

## 10. Refactoring Changes

1. Added telecom_grpc/tls_config.py to isolate secure listener credential construction from Redis/protobuf server imports.
2. Updated telecom_grpc/server.py to call the helper. Missing GRPC_CLIENT_CA now fails closed; client authentication is always enabled on the secure listener. Explicit loopback-only insecure mode remains separate.
3. Added telecom_grpc/tests/test_server_tls.py with tests for missing CA and mandatory client authentication.
4. No model, data, database schema or API contract changed. No historical or generated files were deleted.

## 11. Testing & Verification

| Check | Status | Result |
|---|---|---|
| Active main suite: python -B -m pytest -q -p no:cacheprovider --basetemp=reports/.pytest_tmp tests | PASS | 405 passed in 33.19 s after code change. |
| gRPC mTLS regression suite | PASS | 2 passed in 0.31 s. |
| Root-wide default pytest discovery | FAIL / unsuitable active command | 12 collection errors: eight backup snapshot tests lack their snapshot import roots; three Edu DSS tests need backend root on sys.path; RAM tests need Redis absent from audit interpreter. |
| First main-suite run without workspace basetemp | TESTS COMPLETED; runner teardown error | Reached 100%, but pytest cleanup failed with PermissionError on external pytest-current. Rerun with workspace basetemp passed. |
| Lint/type/static analysis | NOT RUN | No pass claimed. |
| Docker/Redis/Kafka/Spark/Edu integration and service startup | NOT RUN | External services, adapters or certificates were not provisioned. |
| Android build/emulator | NOT RUN | Not part of Python test run. |

## 12. Remaining Issues

- No real carrier traffic, production labels or independently certified model/FPR results.
- Main HTTP p99 30 ms and 50k TPS targets remain unmet in project measurements.
- Main storage uses single-owner SQLite; sample gRPC/Edu Redis deployments are single-node and lack verified failover.
- RAM lookup/gRPC are not wired into main live inference; carrier-specific SIP/IMS adapter is external.
- Voice pipeline exists, but real annotated WAV corpus and certified model are absent; ASR weights are not included.
- Edu DSS requires institutional adapters, identity provider and real governed data; fixture outputs are not valid recommendations.
- Android lacks native call screening and release security proof.
- No full deploy, live network, multi-instance, disaster recovery or production load test was run.
- Root-wide test collection is ambiguous; standalone projects need their own dependency environments.

## 13. Development Roadmap

| Phase / priority | Problem and proposal | Complexity | Modules |
|---|---|---:|---|
| 1 – Foundation / Critical | Synthetic-only evidence -> governed representative labeled data, time/group holdouts, external calibration, shadow evaluation and explicit action costs. | High | src/build_dataset.py, train.py, evaluate.py, statistical_reporting.py, data governance. |
| 1 – Foundation / High | HTTP SLA unmet -> repeat offered-load tests on target hardware, profile owner/SQLite/serialization and define degraded-rate SLO. | High | backend_runtime.py, backend_state.py, server.py, http_loadtest.py. |
| 1 – Foundation / High | Separate online prototype -> decide migration and policy source of truth; prove duplicate/late/recovery semantics before connecting. | High | ram_online/, telecom_grpc/, server.py, carrier adapter. |
| 1 – Foundation / High | Edu adapters missing -> integrate and validate OIDC/JWKS, AutoML/CMS/advice/grade/graph contracts. | High | edu_dss/backend/app/security.py, services.py, main.py, ui/app.py. |
| 2 – Optimization / High | Single-owner SQLite and single-node Redis -> HA topology, backup/restore, retention, migrations and failure drills. | High | backend_state.py, Compose, Redis policy publisher, deployment manifests. |
| 2 – Optimization / Medium | Drift metrics lack causal quality labels -> segment monitoring, adjudicated labels and gated retrain/promotion. | Medium/High | drift.py, shadow_run.py, shadow_feedback.py, model manifests. |
| 2 – Optimization / Medium | Dependency resolution varies -> per-service locks, SBOM, vulnerability process and reproducible models. | Medium | requirements, Dockerfiles, CI. |
| 3 – Production / Critical | Carrier certification absent -> staged mTLS integration, explicit UNKNOWN/fail-open contract, network/chaos/failover tests and operator runbooks. | High | telecom_grpc/, ram_online/, adapter, monitoring. |
| 3 – Production / High | Audit/key operations are local -> redacted observability, external audit sink, key rotation/HSM and retention. | High | security.py, evidence.py, backend_evidence.py, deployment. |
| 3 – Production / High | Demo identity/transport -> SSO/OTP, HTTPS, revocation, privacy review and Android release configuration. | High | static/app/, server.py, android/. |
| 3 – Production / Medium | Test discovery collects snapshots -> pytest config and CI matrix per service/environment. | Medium | pytest configuration, workflows, tests and per-project requirements. |

## 14. Technical Recommendations

1. Keep shadow mode until real-data calibration bounds, operational metrics and operator approval criteria pass.
2. Publish distinct counters for scored, pending, overload, fail-open, durable delivery and actual carrier action.
3. Preserve UNKNOWN as unknown in all adapters; never report fail-open as verified ALLOW.
4. Configure pytest to exclude backups and run independent services in their own dependency environments.
5. Keep backups, datasets, model artifacts and evidence until retention/rollback policy and dependency checks justify removal.
6. Keep production startup fail-closed on missing TLS client CA/keys and lock dependencies. The gRPC client-auth requirement is now enforced.

