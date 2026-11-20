"""Compare the chosen algorithms against common alternatives on identical data and thresholds.

Every candidate is trained on the same train window, gets its block / flag thresholds from the
legitimate events of the same validation window (same FPR budgets as train.py), and is scored on
the independent test window plus the 10 replicate windows of evaluate.py (11 x 50,000 events).
Latency is measured the way the runtime calls a model: one event at a time, preprocessing included.

Run:  python src/compare_models.py      -> data/model_comparison.json + data/model_comparison.md
Note: thresholds here are pure validation quantiles (no FLOORS), so the LightGBM row can differ
slightly from the deployed model; the comparison is like-for-like across candidates.
"""

from __future__ import annotations

import json
import time
import warnings

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.kernel_approximation import Nystroem
from sklearn.linear_model import LogisticRegression, SGDOneClassSVM
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.naive_bayes import ComplementNB
from sklearn.neighbors import LocalOutlierFactor
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC

from build_dataset import make_window, replay
from config import DATA_DIR, TARGET_BLOCK_FPR, TARGET_FLAG_FPR
from evaluate import wilson
from feature_store import AUDIO_FEATURES, CALL_FEATURES
from train import (
    ANOMALY_FEATURES,
    ANOMALY_FLAG_FPR,
    CALL_FLAG_FPR,
    SIGNALLING_FRAUD,
    segment_of,
    sms_matrix,
    threshold_for_fpr,
)

try:
    import xgboost as xgb
except ImportError:  # optional dependency
    xgb = None

warnings.filterwarnings("ignore")
CACHE = DATA_DIR / "compare_cache"
CACHE.mkdir(exist_ok=True)
# Cùng cấu hình cửa sổ với build_dataset.py và replicate_study() của evaluate.py.
WINDOWS = {
    "train": (11, 48, 0, "train", 12, None),
    "val": (22, 24, 0, "val", 12, None),
    "test": (101, 4.3, 8, "test", 24, 50_000),
}
WINDOWS.update({f"rep{s}": (s, 4.3, 8, "test", 24, 50_000) for s in range(201, 211)})
TEST_NAMES = ["test"] + [f"rep{s}" for s in range(201, 211)]


# ------------------------------------------------------------------ data
def features(name):
    """Per-type feature frames of one window, cached as parquet (simulator is seeded, so reproducible)."""
    paths = {k: CACHE / f"{name}_{k}.parquet" for k in ("call", "sms", "audio")}
    if all(p.exists() for p in paths.values()):
        return {k: pd.read_parquet(p) for k, p in paths.items()}
    seed, hours, start, kind, warm_h, limit = WINDOWS[name]
    warm, ev = make_window(name, seed, hours, start, kind, limit=limit, warmup_hours=warm_h)
    out = {k.lower(): df for k, df in replay(ev, warm).items()}
    for k, df in out.items():
        df.to_parquet(paths[k], index=False)
    print(f"  {name}: {len(ev):,} events", flush=True)
    return out


def load_all():
    data = {n: features(n) for n in WINDOWS}
    test = {
        k: pd.concat([data[n][k].assign(window=n) for n in TEST_NAMES], ignore_index=True)
        for k in ("call", "sms", "audio")
    }
    return data["train"], data["val"], test


# ------------------------------------------------------------------ measurement
def latency(score_one, rows, reps=1500):
    """p50 / p99 of scoring a single event (preprocessing included), after a short warm-up."""
    for r in rows[:50]:
        score_one(r)
    lat = []
    for i in range(reps):
        r = rows[i % len(rows)]
        t = time.perf_counter()
        score_one(r)
        lat.append((time.perf_counter() - t) * 1000.0)
    return {"p50_ms": round(float(np.percentile(lat, 50)), 3), "p99_ms": round(float(np.percentile(lat, 99)), 3)}


def rate(hit, n):
    lo, hi = wilson(int(hit), int(n))
    return {
        "n": int(n),
        "hit": int(hit),
        "pct": round(100 * hit / max(n, 1), 3),
        "ci95_pct": [round(100 * lo, 3), round(100 * hi, 3)],
    }


def supervised_report(y, s, th):
    """Recall at the block / flag thresholds, false positives on legitimate events, ranking quality."""
    pos, neg = y == 1, y == 0
    blk, anyf = s >= th["block"], s >= th["flag"]
    return {
        "pr_auc": round(float(average_precision_score(y, s)), 4),
        "roc_auc": round(float(roc_auc_score(y, s)), 5),
        "block_recall": rate((blk & pos).sum(), pos.sum()),
        "block_or_flag_recall": rate((anyf & pos).sum(), pos.sum()),
        "block_fp": rate((blk & neg).sum(), neg.sum()),
        "block_or_flag_fp": rate((anyf & neg).sum(), neg.sum()),
    }


def run_supervised(name, fit, score, Xtr, ytr, Xva, yva, Xte, yte, block_fpr, flag_fpr, one_rows, block_max=False):
    """fit(Xtr, ytr, Xva, yva) -> model; score(model, X) -> 1-D scores (higher = more fraud)."""
    t = time.time()
    m = fit(Xtr, ytr, Xva, yva)
    train_s = time.time() - t
    neg = score(m, Xva)[yva == 0]
    # Ngưỡng chỉ lấy từ sự kiện hợp lệ của validation, giống train.py (audio: block = max hợp lệ).
    th = {
        "block": float(neg.max()) + 1e-9 if block_max else threshold_for_fpr(neg, block_fpr),
        "flag": threshold_for_fpr(neg, flag_fpr),
    }
    rep = supervised_report(yte, score(m, Xte), th)
    rep.update(train_s=round(train_s, 1), latency_single=latency(lambda r: score(m, r), one_rows))
    print(
        f"    {name:<22} PR-AUC {rep['pr_auc']:.4f}  block {rep['block_recall']['pct']:6.2f}%  "
        f"any {rep['block_or_flag_recall']['pct']:6.2f}%  FP(block) {rep['block_fp']['hit']}  "
        f"p99 {rep['latency_single']['p99_ms']} ms",
        flush=True,
    )
    return rep


# ------------------------------------------------------------------ candidates (tabular)
def tabular_candidates(lgb_params, rounds):
    """Same candidate set for the call (Tier 1) and audio (Tier 2) feature tables."""

    def fit_lgb(Xtr, ytr, Xva, yva):
        return lgb.train(
            lgb_params,
            lgb.Dataset(Xtr, ytr),
            num_boost_round=rounds,
            valid_sets=[lgb.Dataset(Xva, yva)],
            callbacks=[lgb.early_stopping(40, verbose=False)],
        )

    def fit_xgb(Xtr, ytr, Xva, yva):
        m = xgb.XGBClassifier(
            n_estimators=rounds,
            learning_rate=0.05,
            max_depth=6,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_lambda=1.0,
            min_child_weight=5,
            tree_method="hist",
            scale_pos_weight=lgb_params.get("scale_pos_weight", 1.0),
            eval_metric="logloss",
            early_stopping_rounds=40,
            n_jobs=-1,
            random_state=7,
        )
        m.fit(Xtr, ytr, eval_set=[(Xva, yva)], verbose=False)
        m.set_params(n_jobs=1)
        return m

    def fit_rf(Xtr, ytr, Xva, yva):
        m = RandomForestClassifier(
            n_estimators=300, min_samples_leaf=3, class_weight="balanced_subsample", n_jobs=-1, random_state=7
        ).fit(Xtr, ytr)
        m.set_params(n_jobs=1)  # one event at a time in production: no thread-pool overhead
        return m

    def fit_lr(Xtr, ytr, Xva, yva):
        return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, class_weight="balanced", max_iter=3000)).fit(
            Xtr, ytr
        )

    def fit_mlp(Xtr, ytr, Xva, yva):
        return make_pipeline(
            StandardScaler(),
            MLPClassifier(hidden_layer_sizes=(64, 32), early_stopping=True, max_iter=200, random_state=7),
        ).fit(Xtr, ytr)

    proba = lambda m, X: m.predict_proba(X)[:, 1]
    cands = [("LightGBM (đang dùng)", fit_lgb, lambda m, X: m.predict(X))]
    if xgb is not None:
        cands.append(("XGBoost", fit_xgb, proba))
    cands += [
        ("Random Forest", fit_rf, proba),
        ("Logistic Regression", fit_lr, proba),
        ("MLP (neural net)", fit_mlp, proba),
    ]
    return cands


# ------------------------------------------------------------------ tiers
def compare_call(tr, va, te):
    print("\n[Tầng 1] Cuộc gọi: LightGBM vs thuật toán khác", flush=True)
    # Giống train_call(): bỏ deepfake (thuộc tầng 2); test còn bỏ zero-day (đánh giá riêng).
    tr, va = tr[tr.fraud_type != "DEEPFAKE_VOICE"], va[va.fraud_type != "DEEPFAKE_VOICE"]
    te = te[~te.fraud_type.isin(["DEEPFAKE_VOICE", "ZERO_DAY_WANGIRI"])]
    y = lambda d: d.fraud_type.isin(SIGNALLING_FRAUD).astype(int).values
    X = lambda d: d[CALL_FEATURES].values.astype(float)
    Xte = X(te)
    one = [Xte[i : i + 1] for i in range(0, len(Xte), max(1, len(Xte) // 2000))]
    params = dict(
        objective="binary",
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=40,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        lambda_l2=1.0,
        scale_pos_weight=10.0,
        verbose=-1,
        seed=7,
    )
    out = {}
    for name, fit, score in tabular_candidates(params, 600):
        out[name] = run_supervised(
            name, fit, score, X(tr), y(tr), X(va), y(va), Xte, y(te), TARGET_BLOCK_FPR, CALL_FLAG_FPR, one
        )
    # Rules-only baseline (no learning): without it the lift of the ML model over the hand-written
    # behavioural rules was unknown. Score = 2 if a behavioural-evidence rule fires, 1 if a weaker
    # signal does (spoofed protected CLI already counts as evidence), else 0.
    out["Chỉ luật (baseline)"] = run_supervised(
        "Chỉ luật (baseline)",
        lambda *a: None,
        rules_score,
        X(tr),
        y(tr),
        X(va),
        y(va),
        Xte,
        y(te),
        TARGET_BLOCK_FPR,
        CALL_FLAG_FPR,
        one,
    )
    return out


def rules_score(_model, X):
    """Rules-only baseline on the CALL feature matrix (columns in CALL_FEATURES order)."""
    from detector import has_behavioural_evidence

    rows = [dict(zip(CALL_FEATURES, r)) for r in np.atleast_2d(X)]
    out = []
    for f in rows:
        if has_behavioural_evidence(f):
            out.append(2.0)
        elif f["incoming_24h"] == 0 and 0 <= f["active_hours_on_network"] < 6 and f["calls_1h"] >= 3:
            out.append(1.0)
        else:
            out.append(0.0)
    return np.array(out)


def compare_zero_day(tr, va, te, lgb_rep_scores):
    print("\n[Zero-day] Wangiri (chưa có trong dữ liệu huấn luyện): Isolation Forest vs thuật toán khác", flush=True)
    sig = lambda d: d.fraud_type.isin(SIGNALLING_FRAUD) | (d.fraud_type == "DEEPFAKE_VOICE")
    clean_tr, clean_va = tr[~sig(tr)], va[~sig(va)]
    te = te[(te.label == 0) | (te.fraud_type == "ZERO_DAY_WANGIRI")]
    is_zd = (te.fraud_type == "ZERO_DAY_WANGIRI").values
    seg_tr, seg_va, seg_te = segment_of(clean_tr), segment_of(clean_va), segment_of(te)
    A = lambda d: d[ANOMALY_FEATURES].values.astype(float)

    # Mỗi ứng viên: fit trên lưu lượng hợp lệ của một nhóm, trả hàm điểm bất thường (cao = lạ).
    def iforest(Xa):
        m = IsolationForest(n_estimators=100, max_samples=min(1024, len(Xa)), random_state=7).fit(Xa)
        return lambda X: -m.score_samples(X)

    def lof(Xa):
        rng = np.random.default_rng(7)
        sub = Xa[rng.choice(len(Xa), min(20_000, len(Xa)), replace=False)]
        sc = StandardScaler().fit(sub)
        m = LocalOutlierFactor(n_neighbors=35, novelty=True).fit(sc.transform(sub))
        return lambda X: -m.score_samples(sc.transform(X))

    def ocsvm(Xa):
        m = make_pipeline(
            StandardScaler(),
            Nystroem(gamma=0.1, n_components=200, random_state=7),
            SGDOneClassSVM(nu=0.01, random_state=7),
        ).fit(Xa)
        return lambda X: -m.decision_function(X)

    def autoencoder(Xa):
        sc = StandardScaler().fit(Xa)
        Z = sc.transform(Xa)
        m = MLPRegressor(hidden_layer_sizes=(8, 4, 8), max_iter=100, early_stopping=True, random_state=7).fit(Z, Z)
        return lambda X: ((m.predict(sc.transform(X)) - sc.transform(X)) ** 2).mean(axis=1)

    out = {}
    for name, make in [
        ("Isolation Forest (đang dùng)", iforest),
        ("Local Outlier Factor", lof),
        ("One-Class SVM (Nyström)", ocsvm),
        ("Autoencoder", autoencoder),
    ]:
        t = time.time()
        scorers, flagged = {}, np.zeros(len(te), dtype=bool)
        score_all = np.zeros(len(te))
        for seg in ["domestic", "foreign", "trunk"]:
            f = make(A(clean_tr[seg_tr == seg]))
            th = threshold_for_fpr(f(A(clean_va[seg_va == seg])), ANOMALY_FLAG_FPR[seg])
            m = seg_te == seg
            s = f(A(te[m]))
            score_all[m] = s - th  # distance to the segment threshold, comparable across segments
            flagged[m] = s >= th
            scorers[seg] = f
        train_s = time.time() - t
        Xd = A(te[seg_te == "domestic"])
        one = [Xd[i : i + 1] for i in range(0, len(Xd), max(1, len(Xd) // 2000))]
        rep = {
            "zero_day_recall": rate((flagged & is_zd).sum(), is_zd.sum()),
            "legit_flagged": rate((flagged & ~is_zd).sum(), (~is_zd).sum()),
            "roc_auc": round(float(roc_auc_score(is_zd, score_all)), 4),
            "train_s": round(train_s, 1),
            "latency_single": latency(scorers["domestic"], one),
        }
        out[name] = rep
        print(
            f"    {name:<28} bắt {rep['zero_day_recall']['pct']:6.2f}%  gắn cờ nhầm {rep['legit_flagged']['pct']:.3f}%  "
            f"p99 {rep['latency_single']['p99_ms']} ms",
            flush=True,
        )
    # Đối chứng: không có mô hình bất thường, chỉ dựa vào ngưỡng flag của LightGBM có giám sát.
    s, th = lgb_rep_scores
    s = s[(te.index.values)]
    hit = s >= th
    out["Chỉ LightGBM (không có mô hình bất thường)"] = {
        "zero_day_recall": rate((hit & is_zd).sum(), is_zd.sum()),
        "legit_flagged": rate((hit & ~is_zd).sum(), (~is_zd).sum()),
    }
    r = out["Chỉ LightGBM (không có mô hình bất thường)"]
    print(f"    {'Chỉ LightGBM':<28} bắt {r['zero_day_recall']['pct']:6.2f}%", flush=True)
    return out


def lgb_flag_scores(tr, va, te_call):
    """Deployed-style LightGBM scores on every test call (used as the zero-day baseline)."""
    tr, va = tr[tr.fraud_type != "DEEPFAKE_VOICE"], va[va.fraud_type != "DEEPFAKE_VOICE"]
    params = dict(
        objective="binary",
        learning_rate=0.05,
        num_leaves=31,
        min_child_samples=40,
        feature_fraction=0.8,
        bagging_fraction=0.8,
        bagging_freq=1,
        lambda_l2=1.0,
        scale_pos_weight=10.0,
        verbose=-1,
        seed=7,
    )
    ytr, yva = tr.fraud_type.isin(SIGNALLING_FRAUD).astype(int), va.fraud_type.isin(SIGNALLING_FRAUD).astype(int)
    m = lgb.train(
        params,
        lgb.Dataset(tr[CALL_FEATURES], ytr),
        num_boost_round=600,
        valid_sets=[lgb.Dataset(va[CALL_FEATURES], yva)],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    th = threshold_for_fpr(m.predict(va[CALL_FEATURES])[yva.values == 0], CALL_FLAG_FPR)
    return m.predict(te_call[CALL_FEATURES]), th


def compare_sms(tr, va, te):
    print("\n[Tầng 2] SMS: TF-IDF + Logistic Regression vs thuật toán khác", flush=True)

    def vec():
        return TfidfVectorizer(
            analyzer="word",
            token_pattern=r"[a-z0-9]+",
            ngram_range=(1, 2),
            min_df=2,
            max_features=40000,
            sublinear_tf=True,
        )

    # Mỗi ứng viên dùng cùng biểu diễn TF-IDF + metadata (sms_matrix của train.py); chỉ bộ phân loại khác.
    def wrap(make_clf, score_fn):
        def fit(dtr, ytr, dva, yva):
            v = vec()
            Xtr = sms_matrix(dtr, v, fit=True)
            clf = make_clf()
            if isinstance(clf, lgb.LGBMClassifier):
                clf.fit(Xtr.astype(np.float32), ytr)
            else:
                clf.fit(Xtr, ytr)
            return v, clf

        return fit, lambda m, d: score_fn(m[1], sms_matrix(d, m[0]))

    proba = lambda c, X: c.predict_proba(X)[:, 1]
    cands = [
        (
            "TF-IDF + LogReg (đang dùng)",
            *wrap(lambda: LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000), proba),
        ),
        (
            "TF-IDF + Linear SVM",
            *wrap(lambda: LinearSVC(C=0.5, class_weight="balanced"), lambda c, X: c.decision_function(X)),
        ),
        ("TF-IDF + Naive Bayes", *wrap(lambda: ComplementNB(alpha=0.3), proba)),
        (
            "TF-IDF + LightGBM",
            *wrap(
                lambda: lgb.LGBMClassifier(
                    n_estimators=300, learning_rate=0.05, num_leaves=31, verbose=-1, random_state=7
                ),
                lambda c, X: c.predict_proba(X.astype(np.float32))[:, 1],
            ),
        ),
    ]
    one = [te.iloc[i : i + 1] for i in range(0, len(te), max(1, len(te) // 2000))]
    out = {}
    for name, fit, score in cands:
        out[name] = run_supervised(
            name,
            fit,
            score,
            tr,
            tr.label.values,
            va,
            va.label.values,
            te,
            te.label.values,
            TARGET_BLOCK_FPR,
            TARGET_FLAG_FPR,
            one,
        )
    return out


def compare_audio(tr, va, te):
    print("\n[Tầng 2] Giọng nói (deepfake): LightGBM vs thuật toán khác", flush=True)
    X = lambda d: d[AUDIO_FEATURES].values.astype(float)
    Xte = X(te)
    one = [Xte[i : i + 1] for i in range(len(Xte))]
    params = dict(
        objective="binary",
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=20,
        feature_fraction=0.9,
        verbose=-1,
        seed=7,
    )
    out = {}
    for name, fit, score in tabular_candidates(params, 800):
        out[name] = run_supervised(
            name,
            fit,
            score,
            X(tr),
            tr.label.values,
            X(va),
            va.label.values,
            Xte,
            te.label.values,
            TARGET_BLOCK_FPR,
            TARGET_FLAG_FPR,
            one,
            block_max=True,
        )
    return out


# ------------------------------------------------------------------ report
def markdown(res, meta):
    L = [
        "# So sánh thuật toán / Model comparison",
        "",
        f"Huấn luyện: cửa sổ train · Ngưỡng: validation · Đánh giá: {meta['test_windows']} cửa sổ test × 50.000 sự kiện "
        f"(dữ liệu giả lập). Độ trễ = chấm **1 sự kiện**, đã gồm tiền xử lý, CPU {meta['cpu']} luồng.",
        "",
    ]

    def sup_table(title, r, note):
        L.extend(
            [
                f"## {title}",
                "",
                note,
                "",
                "| Mô hình | PR-AUC | Bắt khi CHẶN | Bắt khi CHẶN+CỜ | Chặn nhầm (người thật) | Cờ nhầm | Độ trễ p99 | Train |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for k, v in r.items():
            L.append(
                f"| {k} | {v['pr_auc']:.4f} | {v['block_recall']['pct']:.1f}% ({v['block_recall']['hit']}/{v['block_recall']['n']}) "
                f"| {v['block_or_flag_recall']['pct']:.1f}% | {v['block_fp']['hit']} ({v['block_fp']['pct']:.4f}%) "
                f"| {v['block_or_flag_fp']['hit']} | {v['latency_single']['p99_ms']} ms | {v['train_s']} s |"
            )
        L.append("")

    sup_table("Tầng 1 – Cuộc gọi (SIM box, giả mạo số)", res["tier1_call"], "Ngân sách độ trễ 30 ms.")
    L.extend(
        [
            "## Zero-day – Wangiri (không có trong dữ liệu huấn luyện)",
            "",
            "| Mô hình | Bắt được | Người thật bị gắn cờ | ROC-AUC | Độ trễ p99 |",
            "|---|---|---|---|---|",
        ]
    )
    for k, v in res["zero_day"].items():
        L.append(
            f"| {k} | {v['zero_day_recall']['pct']:.1f}% ({v['zero_day_recall']['hit']}/{v['zero_day_recall']['n']}) "
            f"| {v['legit_flagged']['pct']:.3f}% | {v.get('roc_auc', '–')} | "
            f"{v['latency_single']['p99_ms'] if 'latency_single' in v else '–'} ms |"
        )
    L.extend(
        [
            "",
            "Độ trễ Isolation Forest ở đây là bản sklearn; lúc chạy thật project dùng bản Numba "
            "(`fast_iforest.py`), nằm trong p99 0,45 ms của cả tầng 1 (`benchmark_results.json`).",
            "",
        ]
    )
    sup_table("Tầng 2 – SMS lừa đảo", res["sms"], "Ngân sách độ trễ 170 ms.")
    sup_table(
        "Tầng 2 – Giọng nói deepfake", res["tier2_audio"], "Ngân sách độ trễ 170 ms. Ít mẫu, khoảng tin cậy rộng."
    )
    L.append("Khoảng tin cậy 95 % (Wilson) và mọi số liệu chi tiết: `data/model_comparison.json`.")
    return "\n".join(L)


if __name__ == "__main__":
    import os

    t0 = time.time()
    print("Chuẩn bị dữ liệu (lần đầu sẽ sinh lại cửa sổ và lưu cache)…", flush=True)
    tr, va, te = load_all()
    res = {"tier1_call": compare_call(tr["call"], va["call"], te["call"])}
    te_call = te["call"].reset_index(drop=True)
    res["zero_day"] = compare_zero_day(
        tr["call"], va["call"], te_call, lgb_flag_scores(tr["call"], va["call"], te_call)
    )
    res["sms"] = compare_sms(tr["sms"], va["sms"], te["sms"])
    res["tier2_audio"] = compare_audio(tr["audio"], va["audio"], te["audio"])
    meta = {
        "test_windows": len(TEST_NAMES),
        "cpu": os.cpu_count(),
        "runtime_s": round(time.time() - t0, 1),
        "test_events": {k: len(v) for k, v in te.items()},
        "test_fraud": {k: int(v.label.sum()) for k, v in te.items()},
    }
    (DATA_DIR / "model_comparison.json").write_text(
        json.dumps({"meta": meta, **res}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    md = markdown(res, meta)
    (DATA_DIR / "model_comparison.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    print(f"\nXong sau {meta['runtime_s']} s")
