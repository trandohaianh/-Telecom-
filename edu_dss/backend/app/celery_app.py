from celery import Celery
import os
from .config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

celery = Celery("edu_dss", broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND,
                include=["app.tasks.jobs"])
celery.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    task_track_started=True, task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, task_soft_time_limit=240, task_time_limit=300,
    result_expires=3600, broker_transport_options={"visibility_timeout": 3600},
    task_annotations={"edu.student.advice": {"rate_limit": os.getenv("EDU_LLM_TASK_RATE_LIMIT", "5/m")}},
)
