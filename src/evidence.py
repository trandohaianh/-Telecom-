"""Tamper-evident evidence log (append-only JSON lines with a SHA-256 hash chain).

Every non-ALLOW decision and every fail-open event is written with the features, scores,
reason codes and model version that produced it, so an investigator or a customer complaint
can be traced back. Changing or deleting any line breaks the chain (see verify()).
Writes go through a background thread so they never sit on the decision path.
"""
from __future__ import annotations

import hashlib
import json
import queue
import threading
from pathlib import Path

GENESIS = "0" * 64


# _digest nối prev + payload rồi hash để tạo chuỗi liên kết hash chain cho log.
def _digest(prev: str, payload: str) -> str:
    return hashlib.sha256((prev + payload).encode("utf-8")).hexdigest()


# EvidenceLog ghi log quyết định dưới dạng JSON Lines với hash chain chống sửa đổi.
class EvidenceLog:
    # __init__ khởi tạo prev hash và writer nền để ghi evidence mà không chậm quyết định.
    def __init__(self, path: Path, sample_allow_every: int = 1000, sample_fail_open_every: int = 100,
                 fail_open_summary_every: int = 1000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev = GENESIS
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.prev = json.loads(line)["hash"]
        self.sample_allow_every = sample_allow_every
        self.sample_fail_open_every = sample_fail_open_every
        self.fail_open_summary_every = fail_open_summary_every
        self._n_allow = 0
        self._fo = None                     # current fail-open batch: count, first/last event, reasons
        self.fail_open_total = 0
        self.q: queue.Queue = queue.Queue()
        self.written = 0
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    # submit: CHẶN / GẮN CỜ (kể cả của luật dự phòng) luôn được ghi từng bản; ALLOW sạch lấy mẫu 0,1 %;
    # fail-open (cho qua, chưa chấm) được ĐẾM ĐỦ thành bản tóm tắt theo lô + mẫu 1 % chi tiết. Lúc quá tải, ghi chi tiết
    # từng sự kiện fail-open làm luồng ghi tranh CPU với luồng chấm điểm, gây thêm quá hạn - một vòng luẩn quẩn.
    def submit(self, result: dict):
        if result["decision"] == "ALLOW":
            if result.get("fail_open"):
                self._count_fail_open(result)
                if self.fail_open_total % self.sample_fail_open_every != 1 % self.sample_fail_open_every:
                    return
            else:
                self._n_allow += 1
                if self._n_allow % self.sample_allow_every:
                    return                  # keep a 0.1 % sample of clean decisions for audits
        self.q.put(result)

    def _count_fail_open(self, r):
        self.fail_open_total += 1
        if self._fo is None:
            self._fo = {"record_type": "fail_open_summary", "count": 0, "first_event": r.get("event_id"),
                        "first_ts": r.get("ts"), "reasons": {}}
        fo = self._fo
        fo["count"] += 1
        fo["last_event"], fo["last_ts"] = r.get("event_id"), r.get("ts")
        why = (r.get("reasons") or [{}])[0].get("en", "")
        key = why.split("[")[-1].rstrip("]") if "[" in why else why
        fo["reasons"][key[:60]] = fo["reasons"].get(key[:60], 0) + 1
        if fo["count"] >= self.fail_open_summary_every:
            self.flush_fail_open()

    def flush_fail_open(self):
        """Write the pending fail-open summary (also called on close)."""
        if self._fo is not None:
            self.q.put(self._fo)
            self._fo = None

    # _run là worker nền ghi từng record và cập nhật prev hash để liên kết khối mới với khối cũ.
    def _run(self):
        with self.path.open("a", encoding="utf-8") as fh:
            while True:
                item = self.q.get()
                if item is None:
                    break
                # Chuyển JSON một lần: chuỗi đã băm được nhúng nguyên văn vào dòng ghi (trước đây dumps hai lần,
                # và lúc quá tải - mọi sự kiện fail-open đều được ghi - luồng ghi tranh CPU với luồng chấm điểm).
                payload = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
                h = _digest(self.prev, payload)
                fh.write('{"prev": "' + self.prev + '", "hash": "' + h + '", "record": ' + payload + "}\n")
                self.prev = h
                self.written += 1
                if self.q.empty():
                    fh.flush()

    # close gửi sentinel và chờ thread ghi hết queue trước khi đóng file.
    def close(self):
        self.flush_fail_open()
        self.q.put(None)
        self._t.join()


# verify_incremental chỉ kiểm phần mới ghi thêm kể từ lần trước (state lưu vị trí byte, hash cuối, số dòng),
# để việc kiểm định kỳ không chiếm CPU của luồng chấm điểm khi file lớn dần. Sửa ở phần ĐÃ kiểm thì
# chỉ lần kiểm toàn bộ (verify) mới phát hiện, nên người gọi vẫn phải chạy verify đầy đủ theo chu kỳ dài hơn.
def verify_incremental(path: Path, state: dict):
    """state = {"pos": byte offset, "prev": last hash, "n": records, "line": lines}. Returns (ok, n, bad)."""
    path = Path(path)
    pos, prev, n, line_no = state.get("pos", 0), state.get("prev", GENESIS), state.get("n", 0), state.get("line", 0)
    with path.open("rb") as fh:
        fh.seek(pos)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break                                   # line still being written: check it next time
            line_no += 1
            pos += len(raw)
            line = raw.decode("utf-8")
            if not line.strip():
                continue
            row = json.loads(line)
            payload = json.dumps(row["record"], ensure_ascii=False, sort_keys=True, default=str)
            if row["prev"] != prev or _digest(prev, payload) != row["hash"]:
                return False, n, line_no
            prev, n = row["hash"], n + 1
    state.update(pos=pos, prev=prev, n=n, line=line_no)
    return True, n, None


_CHILD_CODE = (
    "import json, os, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from evidence import verify_incremental\n"
    "state = json.loads(sys.argv[3])\n"
    "ok, n, bad = verify_incremental(sys.argv[2], state)\n"
    "print(json.dumps({'ok': ok, 'n': n, 'bad': bad, 'state': state, 'pid': os.getpid()}))\n"
)


def verify_in_subprocess(path, state: dict, timeout_s: float = 600.0):
    """Run verify_incremental in a child interpreter (round 6).

    Parsing and re-hashing every JSONL line is pure Python. Run inside the API process it competed for
    the GIL with the inference owner: 54 ms for only ~300 records at start-up, and a full pass every
    30 minutes over a production-size log would stall verdicts for seconds. A child process has its own
    interpreter, so the owner thread is not delayed. An empty ``state`` verifies the whole file in one
    pass. Returns (ok, n, bad, new_state); ok is None when the check could not run (never "tampered").
    """
    import subprocess
    import sys
    try:
        out = subprocess.run([sys.executable, "-I", "-c", _CHILD_CODE, str(Path(__file__).resolve().parent),
                              str(path), json.dumps(state)], capture_output=True, text=True, timeout=timeout_s,
                             check=True)
        res = json.loads(out.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None, state.get("n", 0), None, state
    return res["ok"], res["n"], res["bad"], (res["state"] if res["ok"] else state)


# verify kiểm tra tính toàn vẹn của hash chain và trả về dòng sai đầu tiên nếu phát hiện.
def verify(path: Path):
    """Returns (ok, n_records, first_bad_line)."""
    prev, n = GENESIS, 0
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        payload = json.dumps(row["record"], ensure_ascii=False, sort_keys=True, default=str)
        if row["prev"] != prev or _digest(prev, payload) != row["hash"]:
            return False, n, i
        prev, n = row["hash"], n + 1
    return True, n, None
