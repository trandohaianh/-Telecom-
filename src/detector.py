"""Runtime two-tier detector: feature store + trained models + decisions + reason codes.

Events are scored in micro-batches: features are computed sequentially (they depend on the
stream state), model inference is vectorised per batch. A batch of 1 is the single-event path.
"""
from __future__ import annotations

import json
import re
import sys
import time

import joblib
import lightgbm as lgb
import numpy as np

import reason_codes as rc
from config import MODEL_DIR, MODEL_VERSION
from fast_iforest import FastIForest
from model_manifest import validate_model_manifest
from feature_store import AUDIO_FEATURES, CALL_FEATURES, PROTECTED, SMS_META_FEATURES, FeatureStore

# ACTIONS ánh xạ cặp (event_type, decision) sang nhãn tiếng Việt/Anh để UI và operator hiểu hành động.
ACTIONS = {
    ("CALL", "BLOCK"): ("Từ chối thiết lập cuộc gọi trước khi đổ chuông", "Reject call set-up before the phone rings"),
    ("CALL", "FLAG"): ("Cho qua + chuyển Tier 2 giám sát trong cuộc gọi + hàng chờ chuyên viên",
                       "Allow + Tier-2 in-call monitoring + analyst queue"),
    ("CALL", "ALLOW"): ("Cho phép", "Allow"),
    ("SMS", "BLOCK"): ("Chặn & cách ly tin nhắn", "Drop and quarantine the SMS"),
    ("SMS", "FLAG"): ("Giao kèm nhãn cảnh báo lừa đảo", "Deliver with a scam-warning label"),
    ("SMS", "ALLOW"): ("Cho phép", "Allow"),
    ("AUDIO", "BLOCK"): ("Ngắt cuộc gọi + gửi SMS cảnh báo cho người nhận", "Disconnect + send a warning SMS to the callee"),
    ("AUDIO", "FLAG"): ("Phát âm báo cảnh báo trong cuộc gọi", "Play an in-call warning tone"),
    ("AUDIO", "ALLOW"): ("Cho phép", "Allow"),
}


from textnorm import normalize as _strip

# Một luồng cho mỗi lần chấm điểm. Mặc định LightGBM / Numba dùng MỌI nhân cho từng lô nhỏ và các luồng chờ quay vòng:
# server demo 300 sự kiện/giây từng chiếm ~4,2 nhân CPU, làm cả máy chậm và đo tải sai. Mở rộng bằng nhiều tiến trình
# (mỗi tiến trình một phân vùng thuê bao, như loadtest.py --partitions), không phải bằng nhiều luồng trong một lô nhỏ.
PREDICT_THREADS = 1
# Luồng ghi bằng chứng chạy song song: không để nó giữ GIL tới 5 ms (mặc định) mỗi lượt, làm lô đang chấm bị trễ.
sys.setswitchinterval(0.001)
# Chia tỉ lệ metadata SMS, phải trùng với train.sms_matrix.
SMS_META_SCALE = np.array([1, 1, 1, 1, 1, 3, 1, 1, 20.0])
try:
    import numba
    numba.set_num_threads(PREDICT_THREADS)
except Exception:  # noqa: BLE001 - numba is optional (FastIForest falls back to NumPy)
    pass

# Trí nhớ thu giọng: người gọi nói < 15 % thời gian, ít nhất 2 nạn nhân khác nhau trong 1 giờ thì chặn trước khi đổ chuông.
HARVEST_MAX_SPEECH = 0.15
HARVEST_MIN_VICTIMS = 2
HARVEST_WINDOW_S = 3600


# Nguyên tắc chặn cứng cuộc gọi: mô hình có thể rất chắc chỉ vì số "mới xuất hiện, không ai gọi đến" - điều cũng đúng với
# nhiều người thật (SIM mới mua, máy người già, SIM phụ). Chặn trước khi đổ chuông vì thế cần ít nhất MỘT bằng chứng hành vi;
# thiếu thì hạ xuống cảnh báo (cuộc gọi vẫn đi, có giám sát tầng 2). Đây là chỗ đổi một ít recall lấy "không chặn nhầm".
def has_behavioural_evidence(f) -> bool:
    return bool(
        f["protected_cli_mismatch"] or f["cell_unknown_radio"] or f["imei_gateway_tac"]
        or f["msisdn_per_imei_24h"] >= 2 or f["imei_per_msisdn_24h"] >= 2      # nhiều SIM / nhiều máy (SIM box)
        or f["calls_10m"] >= 5 or f["calls_1h"] >= 12                          # gọi dồn dập
        or (f["completed_calls_seen"] >= 3 and f["short_call_ratio"] >= 0.6)   # đã thấy nhiều cuộc cực ngắn
        # cụm SIM mới cùng một trạm: ở trạm đông người, 3-5 SIM mới trong 1 giờ là bình thường -> cần cụm lớn VÀ số này gọi liền
        or (f["new_sims_on_cell_1h"] >= 6 and f["calls_10m"] >= 2)
        or f["velocity_kmh"] >= 300                                            # di chuyển phi lý
    )


# Detector là entry point chính: đọc feature store, chạy mô hình và sinh quyết định + lý do giải thích.
class Detector:
    # Khởi tạo mô hình tier 1, tier 2, zero-day và cấu hình feature store/ingest guard cho batch scoring.
    def __init__(self, model_dir=MODEL_DIR, guard=None, shadow=False, ledger=None, keep_features=False, require_manifest=False):
        # guard  : an IngestGuard, or None to keep the raw file-replay behaviour
        # shadow : score and record decisions but never enforce them (pre-production mode)
        # ledger : an ActionLedger, so a replayed decision is executed at most once
        self.guard = guard
        self.shadow = bool(shadow)
        self.ledger = ledger
        # Drift monitoring needs the feature values of *all* traffic, not only of flagged events;
        # production keeps a sample instead of every row.
        self.keep_features = bool(keep_features)
        self.model_manifest = validate_model_manifest(model_dir, require_manifest=require_manifest)
        self.fs = FeatureStore()
        self.call_model = lgb.Booster(model_file=str(model_dir / "tier1_call_lgbm.txt"))
        self.audio_model = lgb.Booster(model_file=str(model_dir / "tier2_audio_lgbm.txt"))
        z = joblib.load(model_dir / "tier1_zero_day_iforest.joblib")
        self.iso = {k: FastIForest(v) for k, v in z["iso"].items()}
        self.iso_feats = z["features"]
        self.iso_med = np.array([z["median"][f] for f in self.iso_feats])
        self.iso_iqr = np.array([z["iqr"][f] for f in self.iso_feats])
        # M10: per-segment scale for zero-day explanations (falls back to the global scale).
        self.iso_seg_scale = {seg: (np.array([v["median"][f] for f in self.iso_feats]),
                                    np.array([v["iqr"][f] for f in self.iso_feats]))
                              for seg, v in (z.get("segment_scale") or {}).items()}
        s = joblib.load(model_dir / "sms_text_lr.joblib")
        self.vec, self.sms_clf = s["vec"], s["clf"]
        self.sms_coef = self.sms_clf.coef_.ravel()
        self.sms_intercept = float(self.sms_clf.intercept_[0])
        # Thông số của TfidfVectorizer đã huấn luyện, cho bản tính tường minh _tfidf.
        self.sms_vocab, self.sms_idf = self.vec.vocabulary_, self.vec.idf_
        self.sms_nt = len(self.sms_idf)
        self.sms_tok = re.compile(self.vec.token_pattern)
        self.sms_ngram = self.vec.ngram_range
        assert self.vec.analyzer == "word" and self.vec.sublinear_tf and self.vec.norm == "l2" and self.vec.use_idf
        self.sms_vocab_inv = {i: t for t, i in self.vec.vocabulary_.items()}
        self.th = json.loads((model_dir / "thresholds.json").read_text())
        self.model_version = self.th.get("model_version", MODEL_VERSION)
        # Trí nhớ theo số cho cuộc gọi thu giọng: caller -> {callee: thời điểm bị gắn cờ trong cuộc gọi}.
        # Chỉ dùng quyết định đã xảy ra (nhân quả), cần >= HARVEST_MIN_VICTIMS người khác nhau mới chặn.
        self.harvest_hits: dict = {}

    # warm_up trả trước chi phí khởi tạo của lần gọi đầu mỗi mô hình (vài chục ms) trên CHÍNH detector này, bằng dữ liệu
    # giả, không đụng tới kho đặc trưng. Thiếu bước này, các lô đầu sau khi khởi động bị quá hạn 30 ms và fail-open.
    def warm_up(self):
        for _ in range(3):
            xc = np.zeros((4, len(CALL_FEATURES)))
            self.call_model.predict(xc, num_threads=PREDICT_THREADS)
            self.call_model.predict(xc, pred_contrib=True, num_threads=PREDICT_THREADS)
            xa = np.zeros((4, len(AUDIO_FEATURES)))
            self.audio_model.predict(xa, num_threads=PREDICT_THREADS)
            self.audio_model.predict(xa, pred_contrib=True, num_threads=PREDICT_THREADS)
            for iso in self.iso.values():
                iso.score(np.zeros((4, len(self.iso_feats))))
            self._tfidf("tai khoan cua quy khach bi tam khoa xac thuc ngay")
        return self

    # prime nạp lịch sử đã có sẵn vào feature store mà không chạy scoring, phục vụ phục hồi state sau restart.
    def prime(self, events):
        """Load history into the feature store without scoring (state restored after a restart)."""
        from build_dataset import prime
        prime(self.fs, events)

    # ------------------------------------------------------------------ guarded entry point
    # process là API triển khai: lọc duplicate/late, áp dụng ledger idempotent và kiểm soát shadow mode.
    def process(self, events, execute=None):
        """Score a batch through the ingestion guard. Returns one result per input event.

        ``score_batch`` is the raw path and assumes every event is new, in order, and delivered
        once. ``process`` is what a deployment calls: duplicates never reach the feature store, an
        event that is later than the allowed lateness is recorded but cannot trigger an action, and
        enforcement goes through the ledger so a replayed decision fires at most once.
        """
        if self.guard is None:
            results = self.score_batch(events)
        else:
            from ingest_guard import ADMITTED
            to_score, statuses = self.guard.filter(events)
            admitted = sorted((i for i, st in enumerate(statuses) if st == ADMITTED),
                              key=lambda i: float(events[i]["ts"]))
            scored = dict(zip(admitted, self.score_batch(to_score) if to_score else []))
            results = []
            for i, (ev, st) in enumerate(zip(events, statuses)):
                if st != ADMITTED:
                    results.append(self._suppressed(ev, st))
                else:
                    r = scored[i]
                    r["ingest"] = st
                    results.append(r)

        for r in results:
            enforce = r.get("ingest", "ADMITTED") == "ADMITTED" and r["decision"] != "ALLOW"
            if self.shadow:
                # The decision is kept for comparison against labels or analyst review; nothing in
                # the network is touched. This is the only safe way to meet live traffic.
                r["enforced"] = False
                r["mode"] = "SHADOW"
            elif enforce and self.ledger is not None:
                r["enforced"] = self.ledger.execute(r, execute)
            else:
                r["enforced"] = bool(enforce)
        return results

    # _suppressed trả về payload an toàn cho event bị từ chối cập nhật state, tránh ghi sai vào feature store.
    def _suppressed(self, ev, status):
        """A result for an event that was not allowed to update the stream state."""
        et = ev["event_type"]
        vi, en = ACTIONS[(et, "ALLOW")]
        return {
            "event_id": ev["event_id"], "event_type": et, "ts": ev["ts"], "decision": "ALLOW",
            "risk": None, "action_vi": vi, "action_en": en, "reasons": [],
            "suppression_reason": "EVENT_TIME_BEHIND_FRONTIER" if status == "LATE" else "DUPLICATE_EVENT",
            **({"call_id": ev["call_id"]} if ev.get("call_id") else {}),
            "tier": 1 if et == "CALL" else 2, "model_version": getattr(self, "model_version", MODEL_VERSION),
            "party": ev.get("caller") or ev.get("sender"), "counterparty": ev.get("callee") or ev.get("recipient"),
            "ingest": status, "scored": False, "feature_store_ms": 0.0, "batch_ms": 0.0,
        }

    # ------------------------------------------------------------------ helpers
    # _decide chuyển điểm risk thành BLOCK / FLAG / ALLOW theo ngưỡng model đã định nghĩa sẵn.
    @staticmethod
    def _decide(p, th):
        return "BLOCK" if p >= th["block"] else ("FLAG" if p >= th["flag"] else "ALLOW")

    # _top_contrib lấy các feature đóng góp mạnh nhất để sinh mã lý do giải thích cho quyết định.
    def _top_contrib(self, booster, X, names, k=3):
        contrib = booster.predict(X, pred_contrib=True, num_threads=PREDICT_THREADS)[:, :-1]
        out = []
        for row in contrib:
            idx = [i for i in np.argsort(-row)[:k] if row[i] > 0]
            out.append([names[i] for i in idx])
        return out

    # _segments chia call theo tuyến nội địa / trunk / ngoại quốc để dùng ngưỡng zero-day tương ứng.
    @staticmethod
    def _segments(X):
        foreign = X[:, CALL_FEATURES.index("caller_foreign")] == 1
        trunk = (X[:, CALL_FEATURES.index("origin_bank_trunk")] == 1) | (X[:, CALL_FEATURES.index("cli_protected")] == 1)
        return np.where(foreign, "foreign", np.where(trunk, "trunk", "domestic"))

    # ------------------------------------------------------------------ main entry
    # score_batch tính feature theo batch, chấm điểm từng loại sự kiện và trả về payload chuẩn hóa.
    def score_batch(self, events):
        """Score atomically: a failed batch leaves feature and cross-modal memory unchanged."""
        t_total = time.perf_counter()
        missing = object()
        callers = {ev.get("caller") for ev in events if ev.get("caller")}
        before = {caller: dict(self.harvest_hits[caller]) if caller in self.harvest_hits else missing
                  for caller in callers}
        try:
            with self.fs.transaction(events):
                results = self._score_batch(events)
                total_ms = round((time.perf_counter() - t_total) * 1000.0, 4)
                for result in results:
                    result["batch_ms"] = total_ms
            return results
        except BaseException:
            for caller, value in before.items():
                if value is missing:
                    self.harvest_hits.pop(caller, None)
                else:
                    self.harvest_hits[caller] = value
            raise

    def _score_batch(self, events):
        t_batch = time.perf_counter()
        results = [None] * len(events)
        calls, smss, audios = [], [], []
        fs_ms, all_features = [], []
        for i, ev in enumerate(events):
            t = time.perf_counter()
            et = ev["event_type"]
            if et == "CALL":
                f = self.fs.call_features(ev)
                # Lúc thiết lập cuộc gọi (luồng song song từ nhà mạng) chưa có thời lượng: CDR đến sau qua commit_cdr.
                if "duration_s" in ev:
                    self.fs.commit_cdr(ev)
                calls.append((i, f))
            elif et == "SMS":
                f = self.fs.sms_features(ev)
                smss.append((i, f))
            else:
                f = self.fs.audio_features(ev)
                audios.append((i, f))
            all_features.append(f)
            fs_ms.append((time.perf_counter() - t) * 1000.0)

        if calls:
            X = np.array([[f[c] for c in CALL_FEATURES] for _, f in calls], dtype=float)
            p = self.call_model.predict(X, num_threads=PREDICT_THREADS)
            A = X[:, [CALL_FEATURES.index(c) for c in self.iso_feats]]
            seg = self._segments(X)
            a = np.zeros(len(calls))
            for sname in set(seg):
                m = seg == sname
                a[m] = self.iso[sname].score(A[m])
            th = self.th["call"]
            a_th = np.array([th["anomaly_flag"][s_] for s_ in seg])
            dec = []
            need_evidence = set()
            for j in range(len(calls)):
                d = self._decide(p[j], th)
                zero_day = d == "ALLOW" and a[j] >= a_th[j]
                d = "FLAG" if zero_day else d
                if d == "BLOCK" and not has_behavioural_evidence(calls[j][1]):
                    d = "FLAG"
                    need_evidence.add(j)
                dec.append(d)
            need = [j for j, d in enumerate(dec) if d != "ALLOW"]
            contrib = dict(zip(need, self._top_contrib(self.call_model, X[need], CALL_FEATURES))) if need else {}
            for j, (i, f) in enumerate(calls):
                reasons = []
                if dec[j] != "ALLOW":
                    if p[j] >= th["flag"]:
                        reasons = rc.render(rc.CALL_REASONS, contrib[j])
                    if a[j] >= a_th[j]:
                        med, iqr = getattr(self, "iso_seg_scale", {}).get(str(seg[j]), (self.iso_med, self.iso_iqr))
                        z = np.abs((A[j] - med) / iqr)
                        top = [self.iso_feats[k] for k in np.argsort(-z)[:2]]
                        code, vi, en = rc.ZERO_DAY
                        reasons.append({"code": code, "vi": vi, "en": en, "feature": "isolation_forest"})
                        reasons += rc.render(rc.CALL_REASONS, top)
                    if j in need_evidence:
                        code, vi, en = rc.NEED_EVIDENCE
                        reasons.append({"code": code, "vi": vi, "en": en, "feature": "block_policy"})
                results[i] = self._result(events[i], "CALL", dec[j], float(p[j]), reasons, f,
                                          anomaly=float(a[j] - a_th[j]), zero_day=bool(dec[j] == "FLAG" and p[j] < th["flag"]))
        if smss:
            idx = [i for i, _ in smss]
            # TF-IDF và hồi quy logistic viết tường minh (cùng con số với vectorizer + predict_proba của sklearn), không
            # dựng DataFrame / ma trận thưa và không kiểm tra dữ liệu mỗi lần gọi - chi phí cố định đó từng chiếm hơn
            # nửa thời gian với lô nhỏ. Có test so khớp với sklearn.
            docs = [self._tfidf(_strip(events[i]["text"])) for i in idx]
            meta = np.array([[f[c] for c in SMS_META_FEATURES] for _, f in smss], dtype=float) / SMS_META_SCALE
            nt = self.sms_nt
            z = np.array([float(v @ self.sms_coef[js]) if len(js) else 0.0 for js, v in docs]) \
                + meta @ self.sms_coef[nt:] + self.sms_intercept
            p = 1.0 / (1.0 + np.exp(-z))
            th = self.th["sms"]
            for j, i in enumerate(idx):
                d = self._decide(p[j], th)
                reasons = []
                if d != "ALLOW":
                    meta_c = meta[j] * self.sms_coef[nt:]
                    names = [SMS_META_FEATURES[k] for k in np.argsort(-meta_c) if meta_c[k] > 0.3][:2]
                    reasons = rc.render(rc.SMS_REASONS, names)
                    js, v = docs[j]
                    txt_c = v * self.sms_coef[js]
                    if txt_c.sum() > 0:
                        top = sorted(range(len(js)), key=lambda q: (-txt_c[q], js[q]))[:3]
                        grams = [self.sms_vocab_inv[js[q]].strip() for q in top if txt_c[q] > 0]
                        code, vi, en = rc.SMS_REASONS["text"]
                        reasons.append({"code": code, "vi": f"{vi}: “{' / '.join(grams)}”",
                                        "en": f"{en}: “{' / '.join(grams)}”", "feature": "text"})
                results[i] = self._result(events[i], "SMS", d, float(p[j]), reasons, smss[j][1])
        if audios:
            X = np.array([[f[c] for c in AUDIO_FEATURES] for _, f in audios], dtype=float)
            p = self.audio_model.predict(X, num_threads=PREDICT_THREADS)
            th = self.th["audio"]
            dec = [self._decide(v, th) for v in p]
            need = [j for j, d in enumerate(dec) if d != "ALLOW"]
            contrib = dict(zip(need, self._top_contrib(self.audio_model, X[need], AUDIO_FEATURES))) if need else {}
            for j, (i, f) in enumerate(audios):
                ev = events[i]
                reasons = rc.render(rc.AUDIO_REASONS, contrib[j]) if dec[j] != "ALLOW" else []
                results[i] = self._result(ev, "AUDIO", dec[j], float(p[j]), reasons, f)

        # Apply cross-modal policy chronologically after vectorised inference, so memory evidence
        # from earlier AUDIO events affects later CALLs equally for every micro-batch size.
        for ev, r, f in zip(events, results, all_features):
            et = ev["event_type"]
            if ev.get("caller") in PROTECTED:
                # Số tổng đài được bảo vệ là nạn nhân bị giả mạo, không bao giờ bị chặn theo trí nhớ thu giọng:
                # vài đoạn AUDIO giả CLI ngân hàng không được làm chặn cả các cuộc gọi thật từ trung kế ngân hàng.
                # Cuộc gọi giả CLI không qua trung kế đã có protected_cli_mismatch và luật dự phòng.
                continue
            if et == "AUDIO" and f["caller_speech_ratio"] < HARVEST_MAX_SPEECH and f["callee_speech_s"] >= 1.0:
                self.harvest_hits.setdefault(ev["caller"], {})[ev["callee"]] = ev["ts"]
            if et in ("CALL", "AUDIO") and self._is_harvester(ev["caller"], ev["ts"]):
                r["decision"] = "BLOCK"
                r["action_vi"], r["action_en"] = ACTIONS[(et, "BLOCK")]
                code, vi, en = rc.HARVEST_MEMORY
                r["reasons"].insert(0, {"code": code, "vi": vi, "en": en, "feature": "harvest_memory"})
                r["evidence_features"] = dict(f)
                if et == "CALL":
                    r["zero_day"] = False

        batch_ms = (time.perf_counter() - t_batch) * 1000.0
        for r, f_ms in zip(results, fs_ms):
            r["feature_store_ms"] = round(f_ms, 4)
            r["batch_ms"] = round(batch_ms, 4)
        return results

    # _tfidf: một tin nhắn -> (chỉ số từ, trọng số TF-IDF) đúng như TfidfVectorizer (word n-gram, tf log, idf, chuẩn L2).
    def _tfidf(self, text):
        toks = self.sms_tok.findall(text.lower() if self.vec.lowercase else text)
        lo, hi = self.sms_ngram
        cnt = {}
        for n_ in range(lo, hi + 1):
            for k in range(len(toks) - n_ + 1):
                j = self.sms_vocab.get(toks[k] if n_ == 1 else " ".join(toks[k:k + n_]))
                if j is not None:
                    cnt[j] = cnt.get(j, 0) + 1
        if not cnt:
            return np.zeros(0, dtype=np.int64), np.zeros(0)
        js = np.fromiter(sorted(cnt), dtype=np.int64, count=len(cnt))
        v = (1.0 + np.log(np.array([cnt[j] for j in js], dtype=float))) * self.sms_idf[js]
        return js, v / np.sqrt(v @ v)

    # _is_harvester: số này đã im lặng thu giọng ở ít nhất HARVEST_MIN_VICTIMS người khác nhau trong 1 giờ qua?
    def _is_harvester(self, caller, ts):
        hits = self.harvest_hits.get(caller)
        if not hits:
            return False
        for callee in [c for c, t in hits.items() if ts - t > HARVEST_WINDOW_S]:
            del hits[callee]
        return sum(1 for t in hits.values() if t <= ts) >= HARVEST_MIN_VICTIMS

    # _result đóng gói kết quả chuẩn hóa để EvidenceLog, ledger và UI dùng chung một schema.
    def _result(self, ev, et, decision, risk, reasons, feats, anomaly=None, zero_day=False):
        vi, en = ACTIONS[(et, decision)]
        out = {
            "event_id": ev["event_id"], "event_type": et, "ts": ev["ts"], "decision": decision,
            "risk": round(risk, 6), "action_vi": vi, "action_en": en, "reasons": reasons,
            "tier": 1 if et == "CALL" else 2, "model_version": getattr(self, "model_version", MODEL_VERSION),
            "party": ev.get("caller") or ev.get("sender"), "counterparty": ev.get("callee") or ev.get("recipient"),
            "scored": True,
            "artifact_status": getattr(self, "model_manifest", {}).get("status", "legacy_unverified"),
            **({"call_id": ev["call_id"]} if ev.get("call_id") else {}),
        }
        if anomaly is not None:
            out["anomaly_score"] = round(anomaly, 4)
            out["zero_day"] = zero_day
        if decision != "ALLOW" or self.keep_features:
            # Monitoring/retraining capture must retain the exact feature values used by inference.
            # Compact evidence keeps its existing display precision; UI formatting is independent.
            out["evidence_features"] = (dict(feats) if self.keep_features else
                                        {k: (round(v, 4) if isinstance(v, float) else v) for k, v in feats.items()})
        return out
