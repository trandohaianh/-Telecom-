"""Role-separated API for the Edu DSS services."""
from __future__ import annotations

import asyncio
import json
import math
import zlib
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from celery.result import AsyncResult
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis

from . import services
from .celery_app import celery
from .config import MAX_BODY_BYTES, REDIS_URL
from .course_access import course_is_authorized
from .request_limits import RequestBodyLimitMiddleware
from .security import Role, require_roles
from .tasks.jobs import personalized_advice, precompute_explanations, train_model


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TrainingRequest(StrictModel):
    dataset_ref: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=128)
    metric: str = Field(default="f1", max_length=32)

    @field_validator("metric")
    @classmethod
    def validate_metric(cls, value: str) -> str:
        allowed = {"f1", "accuracy", "roc_auc"}
        if value not in allowed:
            raise ValueError("metric must be one of: accuracy, f1, roc_auc")
        return value


class CmsRequest(StrictModel):
    course_id: str = Field(min_length=1, max_length=128)
    lecture_id: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=250_000)


class AdviceRequest(StrictModel):
    student_ref: str = Field(min_length=8, max_length=128)  # pseudonymous ID only
    features: dict[str, float] = Field(max_length=128)
    goal: str = Field(min_length=1, max_length=1000)

    @field_validator("features")
    @classmethod
    def validate_finite_features(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not math.isfinite(number) for number in value.values()):
            raise ValueError("feature values must be finite numbers")
        return value


class GradePredictionRequest(StrictModel):
    course_id: str = Field(min_length=1, max_length=128)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.cache = Redis.from_url(REDIS_URL, decode_responses=True,
        socket_connect_timeout=.05, socket_timeout=.05, max_connections=128)
    yield
    await app.state.cache.aclose()


app = FastAPI(title="Edu DSS API", version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


def require_course_access(user, course_id: str) -> None:
    allowed = course_is_authorized(
        user["role"].value, course_id,
        user.get("course_ids", ()), user.get("teaching_course_ids", ()))
    if not allowed:
        raise HTTPException(404, "Course not found")


app.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_BODY_BYTES)


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request):
    try:
        await request.app.state.cache.ping()
    except Exception:
        raise HTTPException(503, "Cache unavailable")
    return {"status": "ready"}


@app.post("/api/instructor/automl", status_code=202)
async def start_training(body: TrainingRequest, user=Depends(require_roles(Role.INSTRUCTOR, Role.ADMIN))):
    task_id = await enqueue_owned(train_model, {**body.model_dump(), "requested_by": user["sub"]}, user["sub"])
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/instructor/leaderboard/{model_ref}")
async def leaderboard(model_ref: str, user=Depends(require_roles(Role.INSTRUCTOR, Role.ADMIN))):
    # Adapter stores leaderboard JSON under this tenant-scoped key.
    key = f"edu:leaderboard:{user['sub']}:{model_ref}"
    value = await app.state.cache.get(key)
    if value is None:
        raise HTTPException(404, "Leaderboard not found")
    return {"model_ref": model_ref, "rows": json.loads(value)}


@app.post("/api/instructor/cms/precompute", status_code=202)
async def cms_precompute(body: CmsRequest, user=Depends(require_roles(Role.INSTRUCTOR, Role.ADMIN))):
    require_course_access(user, body.course_id)
    task_id = await enqueue_owned(precompute_explanations,
        {**body.model_dump(), "requested_by": user["sub"]}, user["sub"])
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/student/explanations/{course_id}/{lecture_id}")
async def read_explanations(course_id: str, lecture_id: str, user=Depends(require_roles(Role.STUDENT, Role.ADMIN))):
    require_course_access(user, course_id)
    key = f"edu:explanation:{course_id}:{lecture_id}"
    value = await app.state.cache.get(key)
    if value is None:
        raise HTTPException(404, "Explanation not prepared")
    return {"cache": "hit", "explanations": json.loads(value)}


@app.post("/api/student/advice", status_code=202)
async def request_advice(body: AdviceRequest, user=Depends(require_roles(Role.STUDENT, Role.ADMIN))):
    if user["role"] == Role.STUDENT and body.student_ref != user["sub"]:
        raise HTTPException(403, "Student advice must use the authenticated student reference")
    compressed = zlib.compress(body.model_dump_json().encode(), level=6)
    if len(compressed) > 16_384:
        raise HTTPException(413, "Compressed advisory input exceeds limit")
    # Keep the Celery payload bounded and JSON serializable. The worker adapter can decompress.
    task_id = await enqueue_owned(personalized_advice,
        {"compressed_json_hex": compressed.hex(), "requested_by": user["sub"]}, user["sub"])
    return {"task_id": task_id, "status": "queued"}


@app.post("/api/student/grade-baseline")
async def predict_grade_baseline(
    body: GradePredictionRequest,
    user=Depends(require_roles(Role.STUDENT)),
):
    require_course_access(user, body.course_id)
    # The identity comes only from the validated JWT. The real adapter resolves
    # trusted academic features for this subject and course.
    request = {"course_id": body.course_id, "student_ref": user["sub"]}
    try:
        return await asyncio.to_thread(services.predict_grade_baseline, request)
    except Exception as exc:
        raise HTTPException(503, "Grade prediction service unavailable") from exc


@app.get("/api/tasks/{task_id}")
async def task_status(task_id: str, request: Request, user=Depends(require_roles(Role.STUDENT, Role.INSTRUCTOR, Role.ADMIN))):
    try:
        UUID(task_id)
    except ValueError:
        raise HTTPException(422, "Invalid task id")
    result = AsyncResult(task_id, app=celery)
    owner = await request.app.state.cache.get(f"edu:task-owner:{task_id}")
    if user["role"] != Role.ADMIN and owner != user["sub"]:
        raise HTTPException(404, "Task not found")
    def snapshot_result():
        state = result.state
        info = result.info
        value = result.result if result.successful() else None
        return state, info if isinstance(info, dict) else {}, value
    state, info, value = await asyncio.to_thread(snapshot_result)
    return {"task_id": task_id, "state": state, "progress": info, "result": value}


@app.get("/api/admin/queue")
async def queue_health(user=Depends(require_roles(Role.ADMIN))):
    inspector = celery.control.inspect(timeout=1.0)
    try:
        active, reserved = await asyncio.to_thread(
            lambda: (inspector.active() or {}, inspector.reserved() or {}))
    except Exception:
        raise HTTPException(503, "Celery control plane unavailable")
    return {"active": sum(map(len, active.values())), "reserved": sum(map(len, reserved.values())),
            "workers": sorted(active)}


@app.get("/api/admin/knowledge-graph")
async def graph_status(user=Depends(require_roles(Role.ADMIN))):
    return services.graph_health()


async def enqueue_owned(task, payload: dict, subject: str) -> str:
    task_id = str(uuid4())
    owner_key = f"edu:task-owner:{task_id}"
    await app.state.cache.set(owner_key, subject, ex=86400)
    try:
        await asyncio.to_thread(task.apply_async, args=[payload], task_id=task_id)
    except Exception as exc:
        # A timeout can happen after the broker accepted the task. Keep its owner
        # record and return the ID so the caller can reconcile instead of blindly
        # retrying and creating duplicate work.
        raise HTTPException(503, detail={
            "message": "Task publication outcome is unknown; check this task before retrying",
            "task_id": task_id,
        }) from exc
    return task_id
