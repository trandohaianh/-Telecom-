"""Append durable decisions to the existing SHA-256 JSONL evidence format."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time

from evidence import GENESIS, verify


def _env_seconds(name, default, lo, hi):
    """Finite float from the environment within [lo, hi]; anything else is a configuration error."""
    import math
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds") from None
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError(f"{name} must be within [{lo}, {hi}] seconds")
    return value


class DurableEvidenceLog:
    def __init__(self, path, state, capacity=8192, sample_allow_every=1000, rotate_bytes=None):
        if type(sample_allow_every) is not int or sample_allow_every < 1:
            raise ValueError("sample_allow_every must be a positive integer")
        if rotate_bytes is not None and (type(rotate_bytes) is not int or rotate_bytes < 1 << 20):
            raise ValueError("rotate_bytes must be an integer >= 1 MiB")
        # Demo only (simulated traffic): the public demo wrote ~1.25 GB of evidence per day with no rotation,
        # and start-up verifies and reads the whole file twice, so a few weeks later a restart ran out of
        # memory. At rotate_bytes the file is renamed to <name>.1<suffix> (one generation kept) and a new
        # chain starts from GENESIS. Live mode never passes rotate_bytes: real evidence retention is a policy.
        self.rotate_bytes = rotate_bytes
        self.rotations = 0
        self.path, self.state = path, state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sample_allow_every = sample_allow_every
        self._demo_allow = 0
        self.q = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._error = None
        self._last_ack_flush = time.monotonic()
        self.prev, self.written = GENESIS, 0
        self.store_id = state.get("store_id")
        policy = state.get("evidence_export_policy")
        if policy is not None and policy != {"sample_allow_every": sample_allow_every}:
            raise RuntimeError("evidence sampling policy changed; explicit migration is required")
        self._exported = set()
        if path.exists():
            try:
                ok, count, bad = verify(path)
            except (ValueError, KeyError) as exc:
                raise RuntimeError("existing evidence is incomplete or invalid; preserve it for investigation") from exc
            if not ok:
                raise RuntimeError(f"evidence chain is invalid at line {bad}; preserve and investigate it")
            self.written = count
            for line in path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                self.prev = row["hash"]
                ref = row["record"].get("journal_ref")
                if ref:
                    self._exported.add(ref)
        # A valid hash-chain prefix cannot reveal that its tail was removed.
        # Durable acknowledgements are an independent expectation for eligible refs.
        self._validate_acknowledged_evidence()
        if self.store_id is None:
            import uuid
            self.store_id = uuid.uuid4().hex
            state.set("store_id", self.store_id)
        if policy is None:
            state.set("evidence_export_policy", {"sample_allow_every": sample_allow_every})
        self.coalesce_s = _env_seconds("FRAUD_EVIDENCE_COALESCE_S", 0.02, 0.0, 5.0)
        self._thread = threading.Thread(target=self._run, name="evidence-writer", daemon=True)
        self._thread.start()

    def _eligible(self, seq, record):
        return "decision" in record and (
            record.get("decision") != "ALLOW" or record.get("fail_open")
            or seq % self.sample_allow_every == 0)

    def _validate_acknowledged_evidence(self):
        after = 0
        while True:
            rows = self.state.acknowledged_evidence_after(after, limit=1000)
            for seq, record in rows:
                if self._eligible(seq, record):
                    ref = f"{self.store_id}:{seq}"
                    if self.store_id is None or ref not in self._exported:
                        raise RuntimeError(
                            f"acknowledged evidence is missing at journal sequence {seq}; "
                            "preserve the database and log for investigation")
            if len(rows) < 1000:
                return
            after = rows[-1][0]

    def submit(self, result):
        if self._error:
            raise RuntimeError("evidence writer failed") from self._error
        # Live results already belong to the durable SQLite outbox.
        if result.get("journal_ref"):
            self._wake.set()
        else:
            if result.get("decision") == "ALLOW" and not result.get("fail_open"):
                self._demo_allow += 1
                if self._demo_allow % self.sample_allow_every:
                    return
            self.q.put(dict(result), timeout=1.0)
            self._wake.set()

    def _append(self, fh, record):
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
        digest = hashlib.sha256((self.prev + payload).encode("utf-8")).hexdigest()
        fh.write(json.dumps({"prev": self.prev, "hash": digest, "record": record},
                            ensure_ascii=False, default=str, allow_nan=False) + "\n")
        self.prev = digest
        self.written += 1

    def rotated_path(self):
        return self.path.with_name(self.path.stem + ".1" + self.path.suffix)

    def _rotate(self, fh):
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.replace(self.path, self.rotated_path())
        self.prev = GENESIS
        self._exported.clear()          # refs of the rotated file; also bounds memory in a long demo run
        self.rotations += 1
        return self.path.open("a", encoding="utf-8")

    def _run(self):
        fh = None
        try:
            fh = self.path.open("a", encoding="utf-8")
            while True:
                if not self._stop.is_set():
                    self._wake.wait(0.1)
                    self._wake.clear()
                    self._stop.wait(self.coalesce_s)  # Coalesce exports; SQLite results are already FULL durable.
                rows = self.state.pending_evidence(limit=1000)
                handled = []
                for seq, record in rows:
                    ref = f"{self.store_id}:{seq}"
                    if ref not in self._exported and self._eligible(seq, record):
                        record = dict(record, journal_ref=ref)
                        self._append(fh, record)
                        self._exported.add(ref)
                    handled.append(seq)
                while not self.q.empty():
                    self._append(fh, self.q.get_nowait())
                if handled or self._stop.is_set() or not self.q.empty():
                    fh.flush()
                    os.fsync(fh.fileno())
                else:
                    fh.flush()
                queued = self.state.queue_evidence_ack(handled) if handled else 0
                if queued >= 1000 or self._stop.is_set() or (
                        not rows and time.monotonic() - self._last_ack_flush >= 1.0):
                    self.state.flush_evidence_acks()
                    self._last_ack_flush = time.monotonic()
                if self.rotate_bytes and fh.tell() >= self.rotate_bytes and not handled:
                    fh = self._rotate(fh)
                if self._stop.is_set() and not rows and self.q.empty():
                    break
                if len(rows) == 1000:
                    self._wake.set()
        except Exception as exc:
            self._error = exc
        finally:
            if fh is not None and not fh.closed:
                fh.close()

    def close(self):
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise RuntimeError("evidence writer is still draining; SQLite outbox retained")
        if self._error:
            raise RuntimeError("evidence writer failed; SQLite outbox retained") from self._error
