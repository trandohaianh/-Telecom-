"""Train the two-tier detectors and pick decision thresholds from the validation window.

Tier 1 (pre-call signalling) : LightGBM on streaming CALL features  + Isolation Forest (zero-day)
SMS content                  : TF-IDF char n-grams + routing metadata -> Logistic Regression
Tier 2 (in-call audio)       : LightGBM on acoustic / transcript features

Thresholds are chosen ONLY from the legitimate events of the validation window so that the
false-positive rate of each action stays under its budget (block <= 0.01 %, flag <= 0.5 %).
The 50,000-event test window is never touched here.
"""

# Nạp thư viện thao tác dữ liệu, huấn luyện mô hình và lưu mô hình.
import json

import joblib
import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.ensemble import IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score

from config import DATA_DIR, MODEL_DIR, MODEL_VERSION, TARGET_BLOCK_FPR, TARGET_FLAG_FPR
from feature_store import AUDIO_FEATURES, CALL_FEATURES, SMS_META_FEATURES
from model_manifest import validate_dataset_manifest, write_model_manifest
from statistical_reporting import fpr_report, threshold_validation

# Khai báo nhóm gian lận nhìn thấy ở tầng báo hiệu và các cấu hình cho huấn luyện.
# families visible in call set-up signalling (voice harvesting: bursts of short answered calls to strangers)
SIGNALLING_FRAUD = {"SIM_BOX", "CLI_SPOOF", "VOICE_HARVEST"}
ANOMALY_FEATURES = [
    "calls_1m",
    "calls_10m",
    "calls_1h",
    "callee_dispersion_1h",
    "in_out_ratio_24h",
    "short_call_ratio",
    "mean_log_duration",
    "completed_calls_seen",
    "imei_per_msisdn_24h",
    "msisdn_per_imei_24h",
    "caller_age_days",
    "velocity_kmh",
    "new_sims_on_cell_1h",
]
# FPR budget of the unsupervised flag, allocated per peer group (foreign traffic is ~2 % of calls,
# so 1 % inside that group costs ~0.02 % of all calls)
ANOMALY_FLAG_FPR = {"domestic": 0.0005, "foreign": 0.01, "trunk": 0.005}
CALL_FLAG_FPR = 0.001  # soft actions on calls trigger Tier-2 monitoring; keep them rare
FLOORS = {
    "call": {"block": 0.5, "flag": 0.05},
    "sms": {"block": 0.9, "flag": 0.5},
    "audio": {"block": 0.9, "flag": 0.5},
}


def segment_of(df):
    """Peer groups for anomaly detection: foreign numbers / bank-agency trunks / domestic subscribers."""
    # Gán mỗi cuộc gọi vào nhóm đối sánh theo cờ số nước ngoài hoặc trunk được bảo vệ.
    return np.where(
        df["caller_foreign"] == 1,
        "foreign",
        np.where((df["origin_bank_trunk"] == 1) | (df["cli_protected"] == 1), "trunk", "domestic"),
    )


def floor(th, kind):
    # Giữ mỗi ngưỡng không thấp hơn mức sàn đã đặt cho tầng và hành động tương ứng.
    return {k: max(v, FLOORS[kind].get(k, 0.0)) for k, v in th.items()}


from textnorm import normalize as strip_accents  # fast Vietnamese accent folding (shared with runtime)  # noqa: E402

# Probabilities never exceed 1.0, so this threshold disables an action while staying finite (JSON-safe).
BLOCK_DISABLED = float(np.nextafter(1.0, np.inf))


def threshold_for_fpr_bound(neg_scores, target_fpr: float, confidence: float = 0.95) -> float:
    """Lowest threshold whose one-sided Clopper-Pearson upper bound on the FPR is <= target (M5).

    threshold_for_fpr() only bounds the POINT estimate (k <= fpr*margin*n), and the shipped reports
    showed upper_95ci_within_target = False for CALL and AUDIO blocks. If even zero false positives
    cannot prove the target with this many negatives (n < ln(1/(1-c))/target, ~29 957 for 1e-4),
    the action is disabled instead of being armed on an unproven bound.
    """
    from scipy.stats import beta

    s = np.sort(np.asarray(neg_scores, dtype=float))[::-1]  # descending
    n = s.size
    if not n or not np.isfinite(s).all():
        raise ValueError("calibration requires finite legitimate scores")
    best = None
    for k in range(n):
        upper = float(beta.ppf(confidence, k + 1, n - k))
        if upper > target_fpr:
            break
        best = k
    if best is None:
        return BLOCK_DISABLED
    # Threshold just above the (best+1)-th largest score: at most `best` negatives reach it (ties too).
    return float(np.nextafter(s[best], np.inf))


def threshold_for_fpr(neg_scores: np.ndarray, fpr: float, margin: float = 0.5) -> float:
    """Smallest threshold whose FPR on legitimate validation events is <= fpr * margin."""
    # Chọn phân vị score của mẫu hợp lệ theo FPR có thêm biên an toàn.
    neg_scores = np.asarray(neg_scores, dtype=float)
    if not neg_scores.size or not np.isfinite(neg_scores).all():
        raise ValueError("calibration requires at least one finite legitimate score")
    if not 0 < fpr < 1 or not 0 < margin <= 1:
        raise ValueError("fpr and safety margin must be within (0, 1)")
    # A fractional expected FP is not permission to spend a whole event. Linear quantiles can
    # admit one FP in a small calibration sample even when the allowed count is strictly zero.
    allowed_false_positives = int(np.floor(fpr * margin * neg_scores.size))
    boundary_index = neg_scores.size - allowed_false_positives - 1
    boundary = float(np.partition(neg_scores, boundary_index)[boundary_index])
    return float(np.nextafter(boundary, np.inf))


def load(name, kind):
    # Đọc bộ đặc trưng parquet theo tên cửa sổ và loại sự kiện.
    return pd.read_parquet(DATA_DIR / f"{name}_{kind}_features.parquet")


def calibration_frame(kind):
    """New builds have an independent calibration window; legacy datasets stay explicit."""
    name = "cal" if (DATA_DIR / f"cal_{kind}_features.parquet").is_file() else "val"
    return load(name, kind), name


def calibration_report(negative_scores, thresholds, name, flag_fpr=TARGET_FLAG_FPR):
    report = threshold_validation(negative_scores, thresholds, TARGET_BLOCK_FPR, flag_fpr)
    report["calibration_window"] = name
    report["independent_of_early_stopping"] = name == "cal"
    report["scope"] = (
        "independent calibration window; held-out evaluation still required"
        if name == "cal"
        else "legacy validation reused for threshold selection and early stopping; unverified"
    )
    return report


def train_call():
    # Nạp tập huấn luyện/kiểm định và loại deepfake vì chúng thuộc tầng âm thanh.
    tr, va = load("train", "call"), load("val", "call")
    # deepfake calls are invisible in signalling: they are handled by Tier 2, not used as Tier-1 labels
    tr = tr[tr.fraud_type != "DEEPFAKE_VOICE"]
    va_eval = va[va.fraud_type != "DEEPFAKE_VOICE"]
    # Tạo nhãn dương chỉ cho các loại gian lận có thể quan sát ở báo hiệu.
    ytr = tr.fraud_type.isin(SIGNALLING_FRAUD).astype(int)
    yva = va_eval.fraud_type.isin(SIGNALLING_FRAUD).astype(int)
    # Khai báo cấu hình LightGBM có trọng số để xử lý mất cân bằng nhãn.
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
    # Đóng gói tập huấn luyện và kiểm định thành Dataset của LightGBM.
    dtr = lgb.Dataset(tr[CALL_FEATURES], ytr)
    dva = lgb.Dataset(va_eval[CALL_FEATURES], yva, reference=dtr)
    # Huấn luyện với dừng sớm khi điểm trên tập kiểm định không còn cải thiện.
    model = lgb.train(
        params, dtr, num_boost_round=600, valid_sets=[dva], callbacks=[lgb.early_stopping(40, verbose=False)]
    )
    # Tính xác suất gian lận trên kiểm định và chỉ lấy score của sự kiện hợp lệ để định ngưỡng.
    p = model.predict(va_eval[CALL_FEATURES])
    ca, calibration_name = calibration_frame("call")
    ca = ca[ca.fraud_type != "DEEPFAKE_VOICE"]
    yca = ca.fraud_type.isin(SIGNALLING_FRAUD).astype(int)
    neg = model.predict(ca[CALL_FEATURES])[yca.values == 0]
    # Chọn ngưỡng block/flag theo ngân sách FPR rồi áp mức sàn cho tầng call.
    th = floor(
        {"block": threshold_for_fpr_bound(neg, TARGET_BLOCK_FPR), "flag": threshold_for_fpr(neg, CALL_FLAG_FPR)}, "call"
    )

    # Zero-day: unsupervised Isolation Forests, one per peer group, trained on legitimate traffic only
    # Tách luồng hợp lệ và gán nhóm riêng để mô hình bất thường không học dữ liệu gian lận.
    clean_tr = tr[ytr.values == 0]
    clean_va = ca[yca.values == 0]
    seg_tr, seg_va = segment_of(clean_tr), segment_of(clean_va)
    # Huấn luyện một Isolation Forest cho từng nhóm và hiệu chỉnh ngưỡng trên mẫu hợp lệ.
    isos, anomaly_validation, th["anomaly_flag"] = {}, {}, {}
    for seg in ["domestic", "foreign", "trunk"]:
        # Chọn các đặc trưng bất thường của nhóm hiện tại để khớp mô hình.
        part = clean_tr[seg_tr == seg][ANOMALY_FEATURES]
        if len(part) < 2:
            # sklearn would fail later with an opaque "max_samples=0"; runtime needs every segment.
            raise ValueError(f"zero-day segment {seg!r} has {len(part)} clean training rows; need at least 2")
        iso = IsolationForest(n_estimators=100, max_samples=min(1024, len(part)), random_state=7)
        iso.fit(part.values)
        # Đổi dấu điểm bình thường thành độ bất thường rồi tìm ngưỡng theo FPR nhóm.
        a_neg = -iso.score_samples(clean_va[seg_va == seg][ANOMALY_FEATURES].values)
        th["anomaly_flag"][seg] = threshold_for_fpr(a_neg, ANOMALY_FLAG_FPR[seg])
        anomaly_validation[seg] = fpr_report(
            int((a_neg >= th["anomaly_flag"][seg]).sum()), len(a_neg), ANOMALY_FLAG_FPR[seg]
        )
        isos[seg] = iso
    # Lưu trung vị và khoảng tứ phân vị để chuẩn hóa đặc trưng trong thành phần suy luận nhanh.
    clean = clean_tr[ANOMALY_FEATURES]
    med = clean.median()
    iqr = (clean.quantile(0.75) - clean.quantile(0.25)).replace(0, 1.0)
    # M10: the zero-day explanation ranks features by |z| inside the event's OWN peer group; global
    # (mostly domestic) scale mislabelled the reason for foreign and trunk traffic.
    seg_scale = {}
    for seg in ["domestic", "foreign", "trunk"]:
        part = clean_tr[seg_tr == seg][ANOMALY_FEATURES]
        if len(part) >= 30:
            seg_scale[seg] = {
                "median": part.median().to_dict(),
                "iqr": (part.quantile(0.75) - part.quantile(0.25)).replace(0, 1.0).to_dict(),
            }

    # Ghi mô hình có giám sát và gói Isolation Forest cùng thống kê chuẩn hóa ra thư mục mô hình.
    model.save_model(str(MODEL_DIR / "tier1_call_lgbm.txt"))
    joblib.dump(
        {
            "iso": isos,
            "median": med.to_dict(),
            "iqr": iqr.to_dict(),
            "features": ANOMALY_FEATURES,
            "segment_scale": seg_scale,
        },
        MODEL_DIR / "tier1_zero_day_iforest.joblib",
    )
    # Tổng hợp chất lượng kiểm định, vòng lặp tốt nhất, ngưỡng và 10 đặc trưng quan trọng nhất.
    rep = {
        "val_pr_auc": average_precision_score(yva, p),
        "val_roc_auc": roc_auc_score(yva, p),
        "best_iteration": model.best_iteration,
        "thresholds": th,
        "threshold_validation": calibration_report(neg, th, calibration_name, CALL_FLAG_FPR),
        "anomaly_threshold_validation": anomaly_validation,
        "threshold_window_reused_for_early_stopping": calibration_name == "val",
        "feature_importance_gain": dict(
            sorted(zip(CALL_FEATURES, model.feature_importance("gain").round(1).tolist()), key=lambda x: -x[1])[:10]
        ),
    }
    # Trả lại ngưỡng vận hành cùng báo cáo huấn luyện để luồng chính ghi ra tệp.
    return th, rep


def sms_matrix(df, vec, fit=False):
    # Chuẩn hóa văn bản tiếng Việt và chuyển nội dung thành vector TF-IDF.
    txt = df.text.map(strip_accents)
    X_txt = vec.fit_transform(txt) if fit else vec.transform(txt)
    # Chuẩn hóa các metadata số rồi ghép với vector văn bản thành ma trận thưa.
    meta = sparse.csr_matrix(df[SMS_META_FEATURES].astype(float).values / np.array([1, 1, 1, 1, 1, 3, 1, 1, 20.0]))
    return sparse.hstack([X_txt, meta]).tocsr()


def train_sms():
    # Đọc tập SMS và cấu hình TF-IDF cho unigram/bigram từ token chữ-số.
    tr, va = load("train", "sms"), load("val", "sms")
    vec = TfidfVectorizer(
        analyzer="word", token_pattern=r"[a-z0-9]+", ngram_range=(1, 2), min_df=2, max_features=40000, sublinear_tf=True
    )
    # Fit bộ vector hóa trên train, sau đó huấn luyện hồi quy logistic có cân bằng lớp.
    Xtr = sms_matrix(tr, vec, fit=True)
    clf = LogisticRegression(C=4.0, class_weight="balanced", max_iter=2000)
    clf.fit(Xtr, tr.label)
    # Tính xác suất trên validation và suy ra ngưỡng từ SMS hợp lệ.
    p = clf.predict_proba(sms_matrix(va, vec))[:, 1]
    ca, calibration_name = calibration_frame("sms")
    neg = clf.predict_proba(sms_matrix(ca, vec))[:, 1][ca.label.values == 0]
    th = floor(
        {"block": threshold_for_fpr_bound(neg, TARGET_BLOCK_FPR), "flag": threshold_for_fpr(neg, TARGET_FLAG_FPR)},
        "sms",
    )
    # Lưu vectorizer, classifier và thứ tự metadata cần cho suy luận.
    joblib.dump({"vec": vec, "clf": clf, "meta": SMS_META_FEATURES}, MODEL_DIR / "sms_text_lr.joblib")
    # Trả ngưỡng cùng các chỉ số phân loại trên cửa sổ validation.
    return th, {
        "val_pr_auc": average_precision_score(va.label, p),
        "val_roc_auc": roc_auc_score(va.label, p),
        "thresholds": th,
        "threshold_validation": calibration_report(neg, th, calibration_name),
    }


def train_audio():
    # Đọc đặc trưng âm thanh và cấu hình LightGBM nhỏ cho tầng phát hiện trong cuộc gọi.
    tr, va = load("train", "audio"), load("val", "audio")
    params = dict(
        objective="binary",
        learning_rate=0.05,
        num_leaves=15,
        min_child_samples=20,
        feature_fraction=0.9,
        verbose=-1,
        seed=7,
    )
    # Huấn luyện trên tập train, theo dõi validation và dừng sớm khi không cải thiện.
    model = lgb.train(
        params,
        lgb.Dataset(tr[AUDIO_FEATURES], tr.label),
        num_boost_round=800,
        valid_sets=[lgb.Dataset(va[AUDIO_FEATURES], va.label)],
        callbacks=[lgb.early_stopping(40, verbose=False)],
    )
    # Chấm điểm validation và lấy điểm của mẫu hợp lệ để đặt ngưỡng.
    p = model.predict(va[AUDIO_FEATURES])
    ca, calibration_name = calibration_frame("audio")
    neg = model.predict(ca[AUDIO_FEATURES])[ca.label.values == 0]
    # Audio negatives are fewer (in-call scoring is sampled), so the block threshold is the max
    # legitimate score on validation (empirical FPR = 0) rather than an extreme quantile.
    # Dùng điểm hợp lệ lớn nhất làm ngưỡng block; ngưỡng flag theo ngân sách FPR.
    th = floor(
        {"block": threshold_for_fpr_bound(neg, TARGET_BLOCK_FPR), "flag": threshold_for_fpr(neg, TARGET_FLAG_FPR)},
        "audio",
    )
    # Lưu mô hình âm thanh và trả về ngưỡng cùng chỉ số đánh giá.
    model.save_model(str(MODEL_DIR / "tier2_audio_lgbm.txt"))
    return th, {
        "val_pr_auc": average_precision_score(va.label, p),
        "val_roc_auc": roc_auc_score(va.label, p),
        "best_iteration": model.best_iteration,
        "thresholds": th,
        "threshold_validation": calibration_report(neg, th, calibration_name),
        "threshold_window_reused_for_early_stopping": calibration_name == "val",
    }


if __name__ == "__main__":
    dataset_provenance = validate_dataset_manifest(DATA_DIR)
    # Chạy đủ ba tầng huấn luyện và nhận ngưỡng/báo cáo riêng cho từng mô hình.
    th_call, r_call = train_call()
    th_sms, r_sms = train_sms()
    th_audio, r_audio = train_audio()
    # Gom ngưỡng theo phiên bản mô hình và ghi cấu hình suy luận.
    thresholds = {"model_version": MODEL_VERSION, "call": th_call, "sms": th_sms, "audio": th_audio}
    (MODEL_DIR / "thresholds.json").write_text(json.dumps(thresholds, indent=2))
    # Ghi báo cáo chất lượng đầy đủ rồi in để tiện theo dõi trong terminal.
    report = {"tier1_call": r_call, "sms": r_sms, "tier2_audio": r_audio}
    (MODEL_DIR / "training_report.json").write_text(json.dumps(report, indent=2, default=float))
    write_model_manifest(MODEL_DIR, MODEL_VERSION, dataset_provenance)
    print(json.dumps(report, indent=2, default=float))
