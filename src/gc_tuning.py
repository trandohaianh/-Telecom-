"""Low-latency garbage-collector settings shared by the HTTP server and the in-process load test.

CPython's cyclic GC periodically scans every tracked object of the oldest generation. After start-up
that includes the models, the 40 000-entry SMS vocabulary, the IsolationForest trees and the primed
feature store, and one full collection stalls the inference owner thread for tens of milliseconds.
The in-process load test always froze that heap; the HTTP server did not, so the serving SLA run
showed sporadic 40-90 ms tails and deadline fail-opens that the in-process benchmark never saw.

gc.freeze() moves the objects alive at start-up into a permanent generation the collector skips.
The higher thresholds make young collections rarer and full ones practically never happen; cyclic
garbage created later is still collected by the young generations. Disable with FRAUD_GC_TUNING=0.
"""
from __future__ import annotations

import gc
import os

THRESHOLDS = (50_000, 50, 1000)


def tune_gc_for_latency() -> bool:
    if os.environ.get("FRAUD_GC_TUNING", "1") == "0":
        return False
    gc.collect()
    gc.freeze()
    gc.set_threshold(*THRESHOLDS)
    return True
