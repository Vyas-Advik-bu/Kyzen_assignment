"""
In-memory job store. One job runs at a time (Semaphore(1)) to avoid
GPU contention — a second request returns 429 with the active job_id.
"""
import asyncio
import uuid
from pathlib import Path
from typing import Any

from app.agent.schemas import ResearchJob
from app.observability.logging import get_logger

log = get_logger(__name__)

_MAX_STORED_JOBS = 20  # LRU eviction when exceeded
_OUTPUT_DIR = Path("outputs")

# Only one concurrent research job — local inference can't handle more.
research_semaphore = asyncio.Semaphore(1)


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ResearchJob] = {}
        self._order: list[str] = []
        self._active_job_id: str | None = None

    def create(self, company_name: str, disable_web_search: bool = False) -> ResearchJob:
        job_id = str(uuid.uuid4())
        job = ResearchJob(job_id=job_id, company_name=company_name,
                          disable_web_search=disable_web_search)
        self._jobs[job_id] = job
        self._order.append(job_id)
        if len(self._order) > _MAX_STORED_JOBS:
            evict = self._order.pop(0)
            self._jobs.pop(evict, None)
            self._delete_excel(evict)
        log.info("job_created", job_id=job_id, company=company_name)
        return job

    @staticmethod
    def _delete_excel(job_id: str) -> None:
        for suffix in ("", "_v2", "_v3"):
            path = _OUTPUT_DIR / f"{job_id}{suffix}.xlsx"
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("excel_delete_failed", path=str(path), error=str(exc))

    def get(self, job_id: str) -> ResearchJob | None:
        return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs: Any) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        for k, v in kwargs.items():
            setattr(job, k, v)

    @property
    def active_job_id(self) -> str | None:
        return self._active_job_id

    def set_active(self, job_id: str | None) -> None:
        self._active_job_id = job_id


job_store = JobStore()
