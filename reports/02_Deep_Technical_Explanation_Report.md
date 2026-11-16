# Deep Technical Explanation & Project Knowledge Report

**Audit date:** 2026-11-13  
**Workspace:** D:/Du_An_1_Telecom_AI_Full/v2

This report explains the implementation that is present in the workspace. “Implemented” means a code path exists; it does not mean carrier deployment or real-world accuracy has been demonstrated. The main fraud model results are principally on simulated traffic. Edu DSS fixtures are explicitly synthetic.

## 1. What problem does this project address?

Telecom Fraud Shield aims to classify calls, SMS messages and selected audio evidence using network-side signals, then return a decision plus reasons to an operator or demo client. It also has mechanisms for signed input, duplicate/late event handling, outage fallback and audit evidence.

The architecture is intentionally positioned as a sidecar: a carrier adapter sends a copy of signaling/CDR information to the service; the service returns a recommendation; the carrier decides how to act. The README says calls do not route through this service. That is an integration design, not evidence of a real carrier link.

Other folders are independent systems:
- ram_online/ and telecom_grpc/ demonstrate low-latency deny-list policy lookup against Redis, optionally through gRPC.
- spark/ processes feature state using Spark and Kafka.
- edu_dss/ is a separate education decision-support service with adapter contracts and synthetic demo data.
- android/ wraps the subscriber PWA in WebView. It is not a native Android call filter.

## 2. System flow from request to response

### Main fraud API

1. run_demo.py:main selects model/data directories, enables demo mode, sets numerical thread limits and imports server.app. It then starts Uvicorn.
2. src/server.py:lifespan creates LiveEngine and its detector, state store, evidence log and worker tasks. Demo mode loads prebuilt event windows and primes feature history. Live mode creates a SQLite state database.
3. Carrier routes call network_request before accepting data. src/security.py:verify_network verifies signed body, timestamp and replay-related constraints. The network API has endpoints for verdicts and completed CDRs.
4. src/backend_runtime.py:InferenceRuntime validates/adopts events, enqueues bounded work and assigns a sequence. One owner thread batches/scorers in order, persists the result transactionally, and then delivers the response. Queue overload is an explicit degraded response rather than hidden successful scoring.
5. src/ingest_guard.py:IngestGuard tracks event identities and event-time order. Duplicate or late events are suppressed so one repeated event does not count as a second call in feature history.
6. src/feature_store.py:FeatureStore.call_features derives the stateful CALL vector from earlier events; other functions derive SMS and audio features. Training and online extraction share the feature definitions.
7. src/detector.py:Detector applies the stored estimators, thresholds, reasons and action policy. src/resilience.py adds the time budget, circuit breaker and fallback.
8. Result and state go through src/backend_state.py:BackendState / SQLite WAL. Evidence is handled by src/backend_evidence.py and src/evidence.py. Delivery and evidence records have distinct semantics: a model suggestion is not proof that an external carrier action happened.
9. The API returns the decision, reason codes and operational status. Browser assets are mounted from static/. Agent proposals are shown separately and require an admin decision.

### CDR and recovery

A CALL setup event can arrive before final duration is known. FeatureStore therefore keeps completed-duration records separately; CDR completion later supplies durations used for future short-call ratio and mean log-duration values. Checkpoint state avoids redoing a long warm-up. InferenceRuntime validates model identity before restoring state and replays journal entries in sequence; previously committed decisions are not blindly enforced again. The recovery code distinguishes an uncertain response-delivery case from new external action.

### Separate Edu DSS flow

Browser -> Streamlit UI -> HTTPX -> FastAPI routes/JWT and course authorization -> Celery task -> service adapter/Redis cache -> task status polling. In demo mode, adapters return deterministic fixtures and explicitly label demo_fixture_only. Without a production adapter, graph status says adapter_required. Student identity is derived from the verified JWT rather than a client-supplied student ID.

## 3. Main modules and their responsibilities

| Module | What it actually does |
|---|---|
| src/config.py | Resolves data/model/log/static paths and operating targets from environment/defaults. |
| src/data_generator.py | Creates synthetic actors/events, number ranges, cells, fraud families and call/SMS/audio-like records. It is a simulator, not a data ingestion connector. |
| src/build_dataset.py | Builds train/validation/test windows and causal feature tables from generated events. |
| src/feature_store.py | Maintains stateful windows and emits the CALL, SMS metadata and AUDIO feature contracts. |
| src/train.py | Fits LightGBM CALL/AUDIO, Isolation Forest anomaly models, and TF-IDF + Logistic Regression for SMS; selects thresholds and writes artifacts. |
| src/detector.py | Loads manifests/models and converts features to scores, reasons and decisions. The library estimators do the model fitting; project code owns feature/decision integration. |
| src/backend_runtime.py | Bounded admission, ordered owner, batching, deadlines, event idempotency, result delivery and recovery orchestration. |
| src/backend_state.py | SQLite tables, WAL transactions, event journal and durable metadata/session state. |
| src/security.py | Roles/session authorization, masking, network HMAC/timestamps, keys and signed audit. |
| src/resilience.py | Circuit breaker, fail-open result and deterministic fallback rules. |
| src/agent.py | Detects operational signals and prepares a signed proposal. Human admin decision is the action boundary. |
| src/voice_pipeline.py | Parses PCM WAV, resamples, estimates speech activity and acoustic descriptors; optional local ASR adapter. |
| src/voice_model.py | Loads a domain/schema-bound model bundle, validates features/output and applies eligible calibrated policy. |
| ram_online/core.py | Redis exact-set policy, Bloom snapshot and version fence. Standalone from main detector. |
| telecom_grpc/server.py | gRPC API around that online gate; telemetry status may be UNKNOWN on outage. Secure TLS credentials now require a client CA. |
| spark/ | Alternative batch/distributed state path. Not a transparent backend switch for the main service. |
| edu_dss/ | Independent education API/UI/job service, with synthetic demo and adapter hooks. |
| android/ | WebView entry point to subscriber web UI. |

## 4. Technology choices and how they are integrated

### Main Python service

FastAPI provides typed HTTP routes and dependency-based access checks in src/server.py. Uvicorn runs the ASGI app; run_demo.py sets thread limits before importing numerical libraries. The custom precise event-loop factory is selected by the launcher. This improves control of local timing but cannot guarantee an operating-system or network deadline.

NumPy/SciPy perform array math, transformations and WAV resampling. pandas/PyArrow handle generated and persisted tables/Parquet. SQLite supplies local ACID transactions, WAL and recovery. These are direct implementations/integrations, not a distributed database stack.

### Model libraries

- LightGBM supplies gradient-boosted tree training and prediction for tabular signal/audio features.
- scikit-learn supplies IsolationForest, TfidfVectorizer and LogisticRegression. The SMS runtime includes a custom sparse scoring path to reduce validation overhead; it relies on the fitted vectorizer/classifier parameters and needs parity protection.
- Numba optionally speeds FastIForest traversal; the project also has a NumPy/Python fallback.
- joblib loads persisted Python model objects. This explains the exact scikit-learn pin: serialization compatibility matters. Model artifacts should only be loaded from trusted locations.

### Other services

Redis is used in two separate ways: policy membership for ram_online/gRPC and cache/queue coordination for Edu DSS. Kafka supplies event transport for streaming experiments; it is not itself an exactly-once transaction with Redis. Spark distributes feature calculation and state by entity. Celery moves Edu jobs out of HTTP request handling. AndroidX libraries support the WebView shell; no OS call-screening API is called by the app.

The gRPC edge uses TLS credentials and may require client CA verification. In the fixed code, secure mode now fails before listening if GRPC_CLIENT_CA is absent. The explicit insecure mode is restricted to loopback. mTLS only proves a certificate chain; production still needs certificate issuance, identity mapping, revocation/rotation and authorization policy.

## 5. Algorithms: theory, implementation and limits

### 5.1 Supervised LightGBM

The task is to estimate fraud probability from a structured row of features. Boosting repeatedly fits a decision tree to improve the current model's loss. For binary labels, predictions are scores transformed/treated as probabilities by the estimator. The project configures learning rate, leaves, minimum child samples, feature/bagging fractions, L2 regularization, seed, and early stopping in src/train.py.

A CALL model sees features such as recent call counts, callee dispersion, device/SIM associations, trunk/radio signals and histories. The source excludes DEEPFAKE_VOICE from signaling labels because the signal tier cannot observe speech content. Audio uses its own tier.

The estimator is library-provided; generation, feature schema, training split, weights, threshold selection, reasons and action policy are project code. Tree ensembles handle nonlinear interactions and mixed feature scales well. They can still learn simulator artifacts and fail under new network behavior. A probability-like score does not by itself mean calibrated probability.

### 5.2 Isolation Forest

The anomaly branch is trained on legitimate CALL rows by peer segment. Each tree chooses random feature splits; an unusual point tends to reach a leaf in fewer splits. The implementation negates normality scores so larger values are treated as more anomalous.

Training roughly scales with number of trees times subsample size times tree depth; scoring roughly scales with number of trees times depth. It can flag novel patterns without fraud labels, but novelty includes rare legitimate behavior. Segment-specific thresholds reduce one kind of population mismatch but do not make anomalies synonymous with fraud.

### 5.3 SMS TF-IDF and logistic regression

src/train.py normalizes Vietnamese text, tokenizes it into words and creates one- and two-token n-grams. TF-IDF weights token frequency against document frequency, then L2-normalizes the sparse vector. Numeric route/sender metadata is scaled and appended.

The classifier computes z = w·x + b and p = 1/(1+e^-z). Example: if a toy input has z=ln(3)≈1.0986, then p=1/(1+1/3)=0.75. That is a toy logistic calculation, not a measured model example. The output is compared with learned BLOCK/FLAG thresholds. Sparse calculation cost depends mostly on nonzero terms. New vocabulary, spelling variation, adversarial text and synthetic training language are limits.

### 5.4 Threshold selection and rare false positives

src/train.py:threshold_for_fpr selects an order statistic from negative calibration scores. It computes allowed_fp=floor(target_fpr × margin × n), then selects the boundary score with NumPy partition and moves the threshold just above the boundary using nextafter.

Example: n=10,000 legitimate calibration records, target FPR=0.0001 and margin=0.5 gives floor(0.5)=0 allowed calibration false positives. The threshold is just above the maximum negative score. This is conservative on that calibration sample; it does not prove a population FPR of zero. Confidence intervals, representative sampling and independent calibration are still required. Floors in train.py may further raise a decision threshold.

The statistical unit can be wrong if records are correlated. docs/VALIDATION_V22.md explicitly cautions about independent-call/event assumptions and states deployment FPR is not certified.

### 5.5 Causal rolling features

FeatureStore uses time-ordered deques and dictionaries rather than recomputing every window from the full event table. For each new call it prunes records older than a horizon, inserts the current event and derives counts/ratios. With ordered timestamps, each record is typically inserted once and removed once, giving amortized near-constant work per event for simple windows. Distinct graph cleanup and per-key maps can add work proportional to active associations.

Examples: calls_1m counts caller events in the last minute; callee_dispersion_1h is distinct callees divided by calls in the hour; in_out_ratio_24h is smoothed as (incoming+1)/(outgoing+1) to avoid division by zero. Current event inclusion and event-time order matter: a replay or late event can inflate a rate, hence IngestGuard.

A finalized call duration is unavailable at setup. Pending CDRs are released into completed duration history once their end timestamp is no later than the current event time. Therefore short_call_ratio describes completed history, not the call currently starting.

### 5.6 Deduplication and lateness

IngestGuard derives a stable event key, records a maximum event timestamp and classifies repeated or older-than-frontier events. It retains keys bounded by event-time TTL. This protects causal feature state from duplicate delivery and avoids applying an out-of-order event after the state frontier has advanced.

The code's strict policy treats any timestamp less than max_ts as late; allowed lateness is represented in the watermark and related stream paths, but this main guard does not buffer every slightly late event for reordering. That is a conservative loss of scoring opportunity in exchange for deterministic state. Verify the contract before changing it.

### 5.7 Haversine mobility proxy

data_generator.py:_haversine computes great-circle surface distance:
a=sin²(Δlatitude/2)+cos(latitude1)cos(latitude2)sin²(Δlongitude/2)
d=2R asin(sqrt(a))
Angles must be radians and R is Earth radius in km. If coordinates match, a=0 and d=0. At the equator, a one-degree longitude change is about 111.2 km for R=6371 km. FeatureStore divides by elapsed time (floored to one minute) and caps at 5000 km/h. This is useful as a synthetic impossible-travel signal; cell centroids/coverage are not exact user location.

### 5.8 PSI drift

src/drift.py builds reference quantile bins, counts shares and computes:
PSI = Σᵢ (Aᵢ-Eᵢ) ln(Aᵢ/Eᵢ)
Eᵢ is reference share; Aᵢ is current share. A small EPS prevents log of zero.

Example: reference E=(0.5,0.5), current A=(0.8,0.2).
Bin 1: (0.8-0.5)ln(0.8/0.5)=0.3 ln(1.6)≈0.1409.
Bin 2: (0.2-0.5)ln(0.2/0.5)=(-0.3)ln(0.4)≈0.2749.
Total ≈0.4159.
This is a distribution-change score, not a model accuracy metric or causal diagnosis. Bin construction, small samples and changing data quality affect interpretation.

### 5.9 Voice WAV features

voice_pipeline.py decodes bounded PCM, optionally resamples to 16 kHz, frames samples, estimates speech with an adaptive energy threshold and computes spectral descriptors. For active frame RMS r, dBFS is 20 log10(r); spectral flatness compares geometric mean power to arithmetic mean power; autocorrelation peaks provide a rough pitch-period proxy. These are handcrafted descriptors. The function explicitly calls the VAD an energy baseline and warns that pitch variation is not clinical jitter or speaker identity.

Model output is abstained to OBSERVE for certain quality warnings. voice_model.py checks feature schema, artifact domain, output shape and probability validity. The model policy can block/flag only when calibration is marked eligible. Without representative labeled audio and independent-group certification, there is no basis to claim real deepfake accuracy.

### 5.10 Bloom filter with exact Redis fence

ram_online/core.py uses a Bloom snapshot to cheaply reject most non-membership candidates locally. Standard sizing is:
m ≈ -n ln(p)/(ln 2)^2 bits
k ≈ (m/n)ln 2 hash positions.
For n=1,000,000 and p=0.0001, m≈19.17 million bits ≈2.29 MiB and k≈13. A Bloom positive may be false and must consult the exact set. In strict mode even negatives use an atomic Redis Lua version/cardinality check, so the filter reduces exact-set work but not the Redis network round trip. Optional gRPC fast-allow can skip Redis only under an explicitly accepted staleness bound. Redis failure yields UNKNOWN/fail-open, not verified ALLOW.

### 5.11 OLS grade demo

edu_dss/backend/app/grade_baseline.py fits seven synthetic (previous GPA, final GPA) pairs with ordinary least squares. For the data in the file, x̄=2.5 and ȳ≈2.5286; slope≈0.8429 and intercept≈0.4214. For previous GPA x=2.5, predicted GPA is 0.4214+0.8429×2.5≈2.5286, returned as 2.53. The input is clamped to 0..4 and output labeled demo_fixture_only. A one-variable trend over seven invented rows is not validated for student intervention or academic outcomes.

## 6. Important design decisions

- **Sidecar, not call path:** API recommendations do not directly carry calls; this avoids making the detector an inline dependency but leaves enforcement to carrier integration.
- **Shadow default:** supports comparison without affecting live decisions; enforce mode requires explicit operator change.
- **SQLite journal/checkpoint:** makes restart and durable response state possible on one host; tradeoff is single-owner and local storage capacity.
- **Causal feature extraction:** online and training feature definitions share code to reduce skew; correctness depends on ordering, late-event policy and same feature contract.
- **Fail-open fallback:** preserves service availability when model misses budget; it deliberately trades fraud protection certainty for availability. Returned operational state must remain visible.
- **Separate voice domain:** real WAV features have their own schema/model contract rather than reusing simulator acoustic columns.
- **Human-reviewed agent:** proposal and decision signatures separate detection from authorization; this is safer for automatic enforcement but still needs role/key operational controls.
- **Separate Redis policy prototype:** deny lookup has a simpler deadline path than scoring models, but it is not transparently part of main API.

## 7. Strengths, weaknesses and failure cases

**Strengths:** explicit shadow mode, bounded queues, idempotency/recovery work, model identity checks, reason codes, input constraints, role checks, signed network payloads, separate voice feature domain, and thorough active regression coverage.

**Weaknesses/limits:** synthetic training evidence, unmet HTTP p99/throughput goals, local/single-owner SQLite state, separate rather than integrated policy services, no real WAV certification, fixture-based Edu DSS, no Android native call interception, incomplete root dependency locking, and absent production carrier integration.

Possible failure cases:
- Queue fills: request is overloaded/fail-open and may remain pending; that is not a scored ALLOW.
- Model timeout/failure: breaker/fallback applies; it can only enforce configured deterministic known rules.
- SQLite commit/delivery deadline: a decision can be durable but response expires; operator must distinguish persistence from a timely actionable response.
- Duplicate CDR/verdict: idempotency should prevent feature double-count/action duplication; event identity conflicts must be rejected.
- Late timestamp: strict frontier may suppress it; changing order policy can alter features and decisions.
- Redis outage: online lookup is UNKNOWN/fail-open; Bloom state alone is not an exact global policy source.
- Stale Bloom in opt-in fast mode: a newly blocked number may be missed within configured snapshot freshness.
- Missing/mismatched model manifest: loader should reject or mark artifact status, depending on configured requirement.
- Correlated evaluation rows: confidence bounds can be overly optimistic if events/speakers/campaigns are not independent.
- Student adapter/claims stale: course access or recommendation can be wrong; production IdP and revocation policy are required.
- Missing mTLS client CA: the fixed secure gRPC server now refuses startup rather than silently permitting unauthenticated clients.

## 8. What is self-built and what is library behavior?

| Capability | Project-owned | Library-provided |
|---|---|---|
| CALL/SMS/AUDIO pipeline | Event schema, generation, feature extraction, splits, thresholds, policy/reason codes and integration | LightGBM, sklearn estimators, NumPy/SciPy operations |
| Fast Isolation Forest | Traversal/scoring optimization and model packaging | Fitted sklearn IsolationForest tree structures |
| SMS inference | Feature normalization/metadata concatenation and direct sparse score path | TF-IDF fitting and LogisticRegression training |
| WAV analysis | Input contract, frame/VAD/descriptor computations and quality gating | SciPy resampling and NumPy FFT/math; optional faster-whisper transcription |
| State and recovery | Queue ownership, journaling, checkpoint/replay and event identity policies | SQLite transaction/WAL engine |
| Policy lookup | Redis Lua policy/version contract, Bloom snapshot lifecycle and fail-open handling | Redis server/client and Kafka client |
| Edu jobs | API contracts, auth checks, task payloads, demo fixtures and adapter HTTP integration | FastAPI, Pydantic, Celery, Redis, Streamlit, HTTPX |
| Distributed features | Feature contract/reconciliation and state commit logic | Spark execution, Kafka transport, Parquet |
| gRPC mTLS | Listener/client policy and environment contract | grpcio TLS and HTTP/2 implementation |

## 9. Verification status and how to explain results

After the mTLS change:
- Main active suite: 405 passed in 33.19 seconds.
- mTLS-specific suite: 2 passed in 0.31 seconds.
- Root-wide default collection is not a valid unified suite: it collects snapshots and fails where separate project environments/import roots/dependencies are absent.
- No lint/type-check, Docker integration, Redis/Kafka/Spark/Edu service startup, Android build or live carrier load test was performed.

A useful oral explanation of the project should distinguish: “code path exists” from “external system connected”, “synthetic test metric” from “real-world performance”, “score” from “verified probability”, “UNKNOWN/fail-open” from “known ALLOW”, and “recommendation” from “carrier action”.

## 10. Next development steps

1. Collect governed, representative call/SMS/audio data with adjudicated outcomes; preserve group and time boundaries.
2. Validate calibration and false-positive bounds on independent traffic; remain in shadow until approved.
3. Meet and reproduce the HTTP latency/throughput target with realistic offered load, restart, overload and outage conditions.
4. Decide whether the standalone Redis/gRPC deny path becomes a separate service or is integrated; define shared policy source and consistency contract.
5. Add production HA, backup/restore, retention, schema migration, monitoring, external audit anchoring and key lifecycle.
6. Integrate Edu DSS identity/domain adapters and establish that fixture mode cannot be confused with live recommendations.
7. Decide the intended Android feature scope; implement native call APIs only after carrier/product and platform requirements are known.

