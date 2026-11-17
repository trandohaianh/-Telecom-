"""Independent real-audio baseline; statistical units and model domains are explicit."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import joblib
import numpy as np
from scipy.stats import beta

from voice_pipeline import REAL_FEATURES, SCHEMA_VERSION

DOMAIN = "real-pcm-wav-v1"
OPTIONAL_PITCH = {"pitch_hz_median", "pitch_period_variation"}


def fpr_upper(false_positives: int, negatives: int, confidence: float = .95) -> float:
    if negatives <= 0 or false_positives >= negatives:
        return 1.0
    return float(beta.ppf(confidence, false_positives + 1, negatives - false_positives))


def calibrate(scores, labels, target_fpr: float, confidence=.95, *, groups=None) -> dict:
    scores = np.asarray(scores, dtype=float)
    original_labels = np.asarray(labels, dtype=float)
    if (scores.ndim != 1 or original_labels.shape != scores.shape
            or not np.isfinite(scores).all() or not np.isfinite(original_labels).all()
            or not np.isin(original_labels, (0, 1)).all()):
        raise ValueError("finite aligned scores and original binary labels are required")
    labels = original_labels.astype(int)
    if not 0 < target_fpr < 1 or not 0 < confidence < 1:
        raise ValueError("invalid calibration target or confidence")
    provided = groups is not None
    if groups is None:
        groups = [str(index) for index in range(len(scores))]
    if len(groups) != len(scores) or any(value is None or not str(value) for value in groups):
        raise ValueError("non-empty independent group IDs must align with scores")
    grouped = {}
    for group, score, label in zip(groups, scores, labels):
        grouped.setdefault(str(group), []).append((float(score), int(label)))
    if any(len({label for _, label in values}) != 1 for values in grouped.values()):
        raise ValueError("calibration group mixes labels; define valid independent calibration units")
    group_scores = np.array([max(score for score, _ in values) for values in grouped.values()])
    group_labels = np.array([values[0][1] for values in grouped.values()])
    negatives = group_scores[group_labels == 0]
    count = len(negatives)
    independent_single_records = provided and all(len(values) == 1 for values in grouped.values())
    policy = {
        "threshold": None, "target_fpr": target_fpr, "confidence": confidence,
        "negatives": count, "negative_groups": count, "negative_records": int(np.sum(labels == 0)),
        "calibration_records": len(scores), "calibration_groups": len(grouped),
        "statistical_unit": "independent_group_any_action",
        "single_record_per_independent_group": independent_single_records,
        "false_positives": 0, "fpr_upper": fpr_upper(0, count, confidence), "eligible": False,
        "eligibility_reason": "insufficient_independent_calibration",
        "assumption": "group identities and independence require operator verification",
    }
    if not count:
        return policy
    candidates = np.r_[np.unique(group_scores), np.nextafter(float(group_scores.max()), np.inf)]
    fps = count - np.searchsorted(np.sort(negatives), candidates, side="left")
    upper = np.ones(len(candidates))
    valid = fps < count
    upper[valid] = beta.ppf(confidence, fps[valid] + 1, count - fps[valid])
    eligible = np.flatnonzero(upper <= target_fpr)
    if len(eligible):
        index = int(eligible[0])
        # M8: a real corpus has several recordings per speaker/call. The bound is computed on the
        # maximum score of each independent group, so clustered groups are valid units as long as the
        # operator supplied them. Without group IDs records are not independent: never eligible.
        policy.update(threshold=float(candidates[index]), false_positives=int(fps[index]),
                      fpr_upper=float(upper[index]), eligible=provided,
                      statistical_unit="independent_group_max" if provided else "unverified_records",
                      eligibility_reason=("independent_unit_bound_met" if independent_single_records
                                          else "clustered_group_max_bound_met") if provided
                                         else "group_ids_required")
    return policy


def feature_vector(observation: dict) -> list[float]:
    if observation.get("schema_version") != SCHEMA_VERSION or observation.get("source") != "real_pcm_wav":
        raise ValueError("the real-audio model only accepts the matching WAV feature schema")
    values = observation.get("features")
    if not isinstance(values, dict) or any(name not in values for name in REAL_FEATURES):
        raise ValueError("all real-audio feature keys are required")
    vector = []
    for name in REAL_FEATURES:
        value = values[name]
        if value is None and name in OPTIONAL_PITCH:
            vector.append(float("nan"))
        else:
            try:
                if isinstance(value, (bool, np.bool_, complex, np.complexfloating)) or not isinstance(value, (int, float, np.number)):
                    raise ValueError("real numeric value required")
                numeric = float(value)
                if not np.isfinite(numeric):
                    raise ValueError("finite value required")
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid real-audio feature: {name}") from exc
            vector.append(numeric)
    return vector


class RealVoiceModel:
    def __init__(self, bundle):
        if bundle.get("domain") != DOMAIN or bundle.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("voice model domain/schema mismatch")
        if bundle.get("features") != REAL_FEATURES:
            raise ValueError("voice model feature order mismatch")
        estimator = bundle.get("estimator")
        if not callable(getattr(estimator, "predict_proba", None)):
            raise ValueError("voice model requires a probability estimator")
        classes = np.asarray(getattr(estimator, "classes_", []))
        if classes.shape != (2,) or set(classes.tolist()) != {0, 1}:
            raise ValueError("voice estimator must expose binary classes 0 and 1")
        self.positive_index = int(np.flatnonzero(classes == 1)[0])
        policies = bundle.get("policies")
        if not isinstance(policies, dict):
            raise ValueError("voice model policies are required")
        for key in ("block", "flag"):
            policy = policies.get(key)
            if not isinstance(policy, dict) or type(policy.get("eligible")) is not bool:
                raise ValueError("voice policy eligibility must be boolean")
            threshold = policy.get("threshold")
            if threshold is not None:
                try:
                    if isinstance(threshold, (bool, complex)) or not isinstance(threshold, (int, float, np.number)):
                        raise ValueError("numeric threshold required")
                    threshold = float(threshold)
                    if not np.isfinite(threshold) or not 0 <= threshold <= np.nextafter(1.0, np.inf):
                        raise ValueError("threshold outside probability range")
                except (OverflowError, TypeError, ValueError) as exc:
                    raise ValueError("invalid voice policy threshold") from exc
            elif policy["eligible"]:
                raise ValueError("eligible voice policy requires a threshold")
        self.bundle = bundle
        self.artifact_id = hashlib.sha256(json.dumps({
            "domain": bundle["domain"], "schema_version": bundle["schema_version"],
            "data_sha256": bundle.get("data_sha256"), "trained_at": bundle.get("trained_at"),
        }, sort_keys=True).encode()).hexdigest()

    @classmethod
    def load(cls, path: Path | str):
        path = Path(path)
        manifest = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if (manifest.get("sha256") != digest or manifest.get("domain") != DOMAIN
                or manifest.get("schema_version") != SCHEMA_VERSION):
            raise ValueError("voice artifact integrity/domain/schema check failed")
        model = cls(joblib.load(path))
        model.artifact_id = digest
        return model

    def predict(self, observation):
        vector = feature_vector(observation)
        warnings = set(observation.get("quality_warnings", []))
        if warnings & {"near_silence", "clipping", "insufficient_speech"}:
            return {"decision": "OBSERVE", "score": None, "reason": "audio_quality_abstention",
                    "recommended_action": "NONE"}
        if "caller_channel_required" in warnings:
            return {"decision": "OBSERVE", "score": None, "reason": "party_attribution_abstention",
                    "recommended_action": "NONE"}
        probabilities = np.asarray(self.bundle["estimator"].predict_proba(np.asarray([vector])))
        if (probabilities.shape != (1, 2) or not np.issubdtype(probabilities.dtype, np.number)
                or not np.isrealobj(probabilities) or not np.isfinite(probabilities).all()
                or np.any(probabilities < 0) or np.any(probabilities > 1)
                or not np.isclose(probabilities.sum(), 1.0, atol=1e-6)):
            return {"decision": "OBSERVE", "score": None, "reason": "invalid_model_output",
                    "recommended_action": "NONE"}
        score = float(probabilities[0, self.positive_index])
        policies = self.bundle["policies"]
        for action, key in (("BLOCK", "block"), ("FLAG", "flag")):
            threshold = policies[key].get("threshold")
            if policies[key].get("eligible") and threshold is not None and score >= threshold:
                return {"decision": action, "score": score, "reason": "heldout_real_audio_model",
                        "recommended_action": action}
        eligible = policies["flag"].get("eligible", False)
        return {"decision": "ALLOW" if eligible else "OBSERVE", "score": score,
                "reason": "below_threshold" if eligible else "insufficient_calibration",
                "recommended_action": "ALLOW" if eligible else "NONE"}
