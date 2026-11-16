# Project 1 · Telecom Fraud Shield
**Real-time AI that detects and stops telecom fraud** · Trần Đỗ Hải Anh · 10/2026

> Updated 2026-11-06: the v2.1 results below are historical simulated results, not the latest evidence. The v2.2 HTTP p99 test did not meet the 30 ms SLA. See [technical defense](TECHNICAL_DEFENSE_VI.md), [SLA report](SLA_TARGET_20261025.md), and [v2.2 validation](VALIDATION_V22.md).

- Demo video (QR in slides/portfolio): https://claude.ai/artifact/1YaojRoGZTyfxCVck7BCy4
- Web portfolio: https://claude.ai/artifact/UGEyhjDe2mh4myfc2vwwyW

## Problem
Product target: return an advisory decision before ringing within <30 ms; the current end-to-end HTTP benchmark has not passed. Fraud prevalence in the simulator is about 0.1%, so false blocking needs a strict budget; attackers keep changing tactics (zero-day).

## Solution
- Causal streaming feature store (1-minute to 24-hour windows, IMEI–SIM graph, mobility)
- CALL: LightGBM + per-group Isolation Forest; SMS: TF-IDF + Logistic Regression; real audio is not validated on a labeled Vietnamese WAV corpus and defaults to OBSERVE/NONE
- BLOCK/FLAG thresholds use a false-positive budget on legitimate calibration samples; reason codes use model contributions and a behavioral evidence gate protects hard blocks
- Fail-open + circuit breaker; SHA-256 hash-chained evidence log; bilingual dashboard

## Historical v2.1 results (simulated; not current metrics)
| Metric | Result |
|---|---|
| Wrong blocks over 500,000 events | 0.0012 % (target < 0.01 %) |
| Fraud blocked or flagged | 85.2 % |
| Never-seen zero-day flagged | 88.8 % |
| Tier-1 p99 latency | 0.45 ms |
| Throughput within SLA | ~25,000 events/s on 2 vCPU (50,000 TPS not yet met) |

**Limits:** v2.1 figures below are simulated; real audio lacks a sufficiently labeled Vietnamese WAV corpus and defaults to OBSERVE/NONE. Older SIM-box and throughput figures are not current commitments; see the v2.2 reports.

**Stack:** Python · FastAPI · LightGBM · scikit-learn · Isolation Forest · Numba · pandas · pytest · Playwright
