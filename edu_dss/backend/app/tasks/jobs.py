"""Replace the explicit pipeline adapter hooks below with project implementations."""
from app.celery_app import celery
from app import services as pipeline
import os


@celery.task(bind=True, name="edu.automl.train")
def train_model(self, request: dict):
    self.update_state(state="PROGRESS", meta={"stage": "validating"})
    result = pipeline.train_automl(request, progress=lambda stage: self.update_state(
        state="PROGRESS", meta={"stage": str(stage)[:120]}))
    return result


@celery.task(bind=True, name="edu.cms.precompute")
def precompute_explanations(self, request: dict):
    self.update_state(state="PROGRESS", meta={"stage": "chunking"})
    return pipeline.precompute_cms(request)


@celery.task(bind=True, name="edu.student.advice")
def personalized_advice(self, request: dict):
    try:
        allowed, wait_seconds = pipeline.reserve_llm_slot(
            int(os.getenv("EDU_LLM_REQUESTS_PER_MINUTE", "60")))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=5, max_retries=12)
    if not allowed:
        raise self.retry(countdown=wait_seconds, max_retries=30)
    return pipeline.personalized_advice(request)
