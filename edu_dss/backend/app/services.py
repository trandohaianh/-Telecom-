"""Explicit integration boundary for the Edu DSS domain pipeline."""
import json
import math
import os
import httpx
import zlib
from pathlib import Path
from redis import Redis
from .cache_keys import explanation_key, leaderboard_key
from .config import REDIS_URL
from .grade_baseline import predict_demo_grade

worker_cache = Redis.from_url(REDIS_URL, decode_responses=True, max_connections=16,
                              socket_connect_timeout=.25, socket_timeout=.5,
                              health_check_interval=30)


def _demo_mode() -> bool:
    return os.getenv("EDU_DEMO_MODE", "0").strip() == "1"

_LLM_RATE_LUA = """
local t = redis.call('TIME')
local sec = tonumber(t[1])
local bucket = math.floor(sec / 60)
local key = KEYS[1] .. ':' .. bucket
local count = redis.call('INCR', key)
if count == 1 then redis.call('EXPIRE', key, 120) end
return {count, 60 - (sec % 60)}
"""


def _adapter_headers() -> dict[str, str]:
    path = os.getenv("EDU_ADAPTER_TOKEN_FILE", "/run/secrets/edu_adapter_token")
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("EDU_ADAPTER_TOKEN_FILE is unavailable") from exc
    if len(token) < 32:
        raise RuntimeError("adapter bearer secret must contain at least 32 characters")
    return {"Authorization": f"Bearer {token}"}


def reserve_llm_slot(limit_per_minute: int) -> tuple[bool, int]:
    """Global atomic fixed-minute limiter shared by all worker replicas."""
    count, wait_seconds = worker_cache.eval(_LLM_RATE_LUA, 1, "edu:llm:requests")
    return int(count) <= limit_per_minute, max(1, int(wait_seconds))


def train_automl(request, progress):
    """Call the institution's isolated training service; never load user code in the API."""
    url = os.getenv("EDU_AUTOML_ADAPTER")
    if not url:
        if not _demo_mode():
            raise RuntimeError("EDU_AUTOML_ADAPTER is not configured")
        progress("demo_fixture_only")
        model_ref = "demo-baseline-v1"
        leaderboard = [
            {"model": "DummyClassifier", "metric": 0.50, "note": "synthetic demo fixture"},
            {"model": "LogisticRegression", "metric": 0.68, "note": "synthetic demo fixture"},
        ]
        key = leaderboard_key(request["requested_by"], model_ref)
        worker_cache.set(key, json.dumps(leaderboard, separators=(",", ":")), ex=3600)
        return {"model_ref": model_ref, "leaderboard_rows": len(leaderboard), "mode": "demo"}
    progress("training")
    with httpx.Client(timeout=httpx.Timeout(240, connect=5)) as client:
        response = client.post(url, json=request, headers=_adapter_headers())
        response.raise_for_status()
        result = response.json()
    key = leaderboard_key(request["requested_by"], result["model_ref"])
    worker_cache.set(key, json.dumps(result.get("leaderboard", []), separators=(",", ":")), ex=30 * 86400)
    return {"model_ref": result["model_ref"], "leaderboard_rows": len(result.get("leaderboard", []))}


def precompute_cms(request):
    """Chunk lecture text and cache domain adapter output; fail closed on missing adapter."""
    text = request.get("content", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("content is required")
    chunks = [text[i:i + 3000] for i in range(0, len(text), 2800)]
    # Adapter is intentionally explicit: generic API does not invent educational explanations.
    adapter = os.getenv("EDU_EXPLANATION_ADAPTER")
    if not adapter:
        if not _demo_mode():
            raise RuntimeError("EDU_EXPLANATION_ADAPTER is not configured")
        explanations = [{
            "chunk": index + 1,
            "summary": chunk[:320],
            "questions": ["Ý chính của đoạn này là gì?", "Có thể áp dụng khái niệm này ở đâu?"],
            "mode": "demo_fixture_only",
        } for index, chunk in enumerate(chunks)]
        key = explanation_key(request["course_id"], request["lecture_id"])
        worker_cache.set(key, json.dumps(explanations, ensure_ascii=False, separators=(",", ":")), ex=86400)
        return {"cache_key": key, "chunks": len(explanations), "mode": "demo"}
    with httpx.Client(timeout=httpx.Timeout(45, connect=5)) as client:
        response = client.post(adapter, json={"course_id": request["course_id"],
            "lecture_id": request["lecture_id"], "chunks": chunks}, headers=_adapter_headers())
        response.raise_for_status()
        explanations = response.json()["explanations"]
    key = explanation_key(request["course_id"], request["lecture_id"])
    worker_cache.set(key, json.dumps(explanations, separators=(",", ":")), ex=86400)
    return {"cache_key": key, "chunks": len(explanations)}


def personalized_advice(request):
    adapter = os.getenv("EDU_ADVICE_ADAPTER")
    if not adapter:
        if not _demo_mode():
            raise RuntimeError("EDU_ADVICE_ADAPTER is not configured")
        body = json.loads(zlib.decompress(bytes.fromhex(request["compressed_json_hex"])))
        return {"mode": "demo_fixture_only", "student_ref": body["student_ref"],
            "plan": ["Ôn tập kiến thức nền", "Làm bài luyện tập ngắn", "Tự đánh giá sau 7 ngày"],
            "input_goal": body["goal"], "features_received": sorted(body["features"])}
    # Do not send raw student records or identity to the LLM adapter.
    body = json.loads(zlib.decompress(bytes.fromhex(request["compressed_json_hex"])))
    with httpx.Client(timeout=httpx.Timeout(45, connect=5)) as client:
        response = client.post(adapter, json={"student_ref": body["student_ref"],
            "features": body["features"], "goal": body["goal"]}, headers=_adapter_headers())
        response.raise_for_status()
        return response.json()


def predict_grade_baseline(request):
    """Use a real adapter when configured; synthetic OLS is available only in demo mode."""
    adapter = os.getenv("EDU_GRADE_ADAPTER")
    if not adapter:
        if not _demo_mode():
            raise RuntimeError("EDU_GRADE_ADAPTER is not configured")
        result = predict_demo_grade(2.5)
        return {
            **result,
            "course_id": request["course_id"],
            "demo_input": {"synthetic_previous_gpa": 2.5},
            "uncertainty": None,
        }

    with httpx.Client(timeout=httpx.Timeout(12, connect=2)) as client:
        response = client.post(adapter, json={
            "course_id": request["course_id"],
            "student_ref": request["student_ref"],
        }, headers=_adapter_headers())
        response.raise_for_status()
        result = response.json()
    prediction = result.get("predicted_grade") if isinstance(result, dict) else None
    if (isinstance(prediction, bool) or not isinstance(prediction, (int, float))
            or not math.isfinite(prediction) or not 0 <= prediction <= 4):
        raise RuntimeError("grade adapter returned an invalid 0..4 prediction")
    return {
        "predicted_grade": round(float(prediction), 2),
        "scale": "0_to_4",
        "course_id": request["course_id"],
        "model": str(result.get("model_ref", "external_grade_adapter"))[:128],
        "mode": "adapter",
        "uncertainty": None,
    }


def graph_health():
    if _demo_mode():
        return {"status": "demo_fixture_only", "nodes": 4, "edges": 3}
    return {"status": "adapter_required", "nodes": None, "edges": None}
