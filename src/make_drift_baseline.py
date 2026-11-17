"""Record drift reference from the independent calibration window (legacy fallback: validation).

Run after training:   python src/make_drift_baseline.py   ->  models/drift_baseline.json

The reference is the same window the decision thresholds were fitted on, which is the only honest
choice: drift is measured against what the model was calibrated for, not against the training set
it memorised. Only bin edges and bin shares are written out, never any row, so the baseline file
contains no subscriber data and can live in a repository.
"""
# Nạp thư viện huấn luyện/đọc dữ liệu và hàm cấu hình drift dùng chung.
from __future__ import annotations

import json

import numpy as np
import lightgbm as lgb
import pandas as pd

import drift
from config import DATA_DIR, MODEL_DIR
from detector import has_behavioural_evidence
from feature_store import CALL_FEATURES
from model_manifest import validate_dataset_manifest, validate_model_manifest

# The features worth watching: behavioural counters that move with real traffic. Static flags
# (origin_local, network_2g, ...) are configuration, not behaviour, and would only add noise.
WATCHED = ["calls_1m", "calls_10m", "calls_1h", "callee_dispersion_1h", "in_out_ratio_24h",
           "incoming_24h", "short_call_ratio", "mean_log_duration", "completed_calls_seen",
           "imei_per_msisdn_24h", "msisdn_per_imei_24h", "velocity_kmh", "new_sims_on_cell_1h",
           "caller_age_days", "active_hours_on_network"]


def main():
    # Đọc đặc trưng validation, mô hình call đã huấn luyện và ngưỡng vận hành.
    # D7: the reference must look like production traffic. "ref" has realistic prevalence; the older
    # cal/val windows are fraud-enriched (BLOCK band ~16x too wide) and made every report INVESTIGATE.
    reference_name = next(name for name in ("ref", "cal", "val") if (DATA_DIR / f"{name}_call_features.parquet").is_file())
    provenance = validate_dataset_manifest(DATA_DIR)
    artifact = validate_model_manifest(MODEL_DIR)
    va = pd.read_parquet(DATA_DIR / f"{reference_name}_call_features.parquet")
    model = lgb.Booster(model_file=str(MODEL_DIR / "tier1_call_lgbm.txt"))
    th = json.loads((MODEL_DIR / "thresholds.json").read_text())["call"]

    # Chấm điểm cửa sổ chuẩn và phân lớp từng score theo ngưỡng call.
    scores = model.predict(va[CALL_FEATURES])
    raw = ["BLOCK" if s >= th["block"] else ("FLAG" if s >= th["flag"] else "ALLOW") for s in scores]
    # Live counters are POST-policy decisions (a BLOCK without behavioural evidence becomes FLAG),
    # so the reference decision rates must be too; raw-threshold rates were not comparable (M3).
    rows = va[CALL_FEATURES].to_dict("records")
    decisions = ["FLAG" if d == "BLOCK" and not has_behavioural_evidence(r) else d for d, r in zip(raw, rows)]

    # Tạo baseline phân phối từ đặc trưng theo dõi, điểm số và các quyết định tương ứng.
    baseline = drift.build_baseline(
        features={f: va[f].values for f in WATCHED if f in va.columns},
        scores=scores,
        decisions=decisions,
        thresholds=th,
        segments=np.where(va["caller_foreign"] == 1, "foreign",
                          np.where((va["origin_bank_trunk"] == 1) | (va["cli_protected"] == 1), "trunk", "domestic")),
    )
    # Gắn nhãn cửa sổ và quy mô, lưu baseline, rồi lấy PSI nền cao nhất nếu có.
    baseline["window"] = reference_name
    baseline["dataset_provenance_status"] = provenance["status"]
    baseline["model_artifact_status"] = artifact["status"]
    baseline["reference_scope"] = ("synthetic reference window, realistic prevalence, known families only; compare matched time/peer groups"
                                   if reference_name == "ref" else
                                   "synthetic threshold-calibration window, enriched fraud prevalence; compare matched time/peer groups")
    baseline["top_feature_coverage"] = "includes active_hours_on_network"
    baseline["n_events"] = int(len(va))
    drift.save(baseline, MODEL_DIR / "drift_baseline.json")
    noise = max(baseline.get("null_psi", {}).values(), default=0.0)
    # In đường dẫn và tóm tắt số đặc trưng, độ nhiễu nền và tỷ trọng các dải quyết định.
    print(f'wrote {MODEL_DIR / "drift_baseline.json"}  '
          f'({len(baseline["features"])} features, {baseline["n_events"]:,} validation calls)')
    print(f'  noise floor (random split of the same window): max PSI {noise:.4f}')
    print(f'  reference decision bands: {baseline["band_shares"]}')


if __name__ == "__main__":
    main()
