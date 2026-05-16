"""
FastAPI application: research job management + SSE streaming.
"""
import asyncio
from contextlib import asynccontextmanager

import re

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.agent.orchestrator import run_pipeline
from app.agent.schemas import ResearchRequest, ResearchJob
from app.config import settings
from app.jobs.store import job_store, research_semaphore
from app.observability.logging import configure_logging, get_logger
from app.streaming.bus import event_bus
from app.streaming.events import error_event

configure_logging(settings.log_level)
log = get_logger(__name__)

_SAFE_FILENAME_RE = re.compile(r"[^\w\s-]")


def _safe_download_filename(company_name: str) -> str:
    """Return an ASCII-safe filename, preventing header injection."""
    safe = _SAFE_FILENAME_RE.sub("", company_name).strip()
    safe = re.sub(r"\s+", "_", safe)
    return (safe[:80] or "research") + "_research.xlsx"


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("startup", primary_model=settings.primary_model,
             fallback_model=settings.fallback_model)
    yield
    log.info("shutdown")


app = FastAPI(title="Company Research Agent", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/research", response_model=ResearchJob, status_code=202)
async def start_research(body: ResearchRequest) -> ResearchJob:
    """
    Start a new research job.
    Returns 429 if another job is already running — single-GPU constraint.
    Acquires the semaphore here (not inside the task) to eliminate the TOCTOU
    race where two fast requests both pass the check before either task starts.
    """
    if research_semaphore.locked():
        active = job_store.active_job_id
        raise HTTPException(
            status_code=429,
            detail={"message": "A research job is already running", "active_job_id": active},
        )

    # Acquire synchronously in the handler — no await between check and acquire,
    # so no context switch can slip another request through.
    await research_semaphore.acquire()

    job = job_store.create(body.company_name, disable_web_search=body.disable_web_search)
    job_store.set_active(job.job_id)
    job_store.update(job.job_id, status="running")

    async def _run() -> None:
        try:
            await run_pipeline(job, event_bus)
        finally:
            job_store.set_active(None)
            research_semaphore.release()

    asyncio.create_task(_run())
    return job


@app.get("/research/{job_id}/stream")
async def stream_research(
    job_id: str,
    request: Request,
    last_event_id: str | None = Header(None),
):
    """
    SSE stream of agent events for a job.
    Supports reconnection via Last-Event-ID header — replays missed events.
    The generator is wrapped in try/finally to guarantee a terminal event is
    always sent, even if an unhandled exception occurs mid-stream (the HTTP 200
    is already committed, so we can't send a 500, but we can send an error event).
    """
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # -1 means "send everything"; non-negative is the last seq the client already has
    replay_from = int(last_event_id) if last_event_id and last_event_id.isdigit() else -1

    async def _generator():
        try:
            async for event in event_bus.subscribe(job_id, last_event_id=replay_from):
                if await request.is_disconnected():
                    log.info("client_disconnected", job_id=job_id)
                    break
                yield ServerSentEvent(
                    data=event.model_dump_json(),
                    event=event.type,
                    id=str(event.seq),
                )
        except Exception as exc:
            log.exception("stream_generator_error", job_id=job_id, error=str(exc))
            # Guarantee the frontend receives a terminal event — never leave it hanging.
            seq = event_bus.next_seq(job_id)
            err = error_event(job_id, seq, f"Internal stream error: {exc}")
            yield ServerSentEvent(data=err.model_dump_json(), event=err.type, id=str(err.seq))

    return EventSourceResponse(_generator())


@app.get("/research/{job_id}")
async def get_research(job_id: str) -> ResearchJob:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/research/{job_id}/excel")
async def download_excel(job_id: str):
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.excel_ready:
        raise HTTPException(status_code=404, detail="Excel not ready yet")

    from pathlib import Path
    for suffix in ["", "_v2", "_v3"]:
        path = Path(f"outputs/{job_id}{suffix}.xlsx")
        if path.exists():
            return FileResponse(
                path=str(path),
                filename=_safe_download_filename(job.company_name),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    raise HTTPException(status_code=404, detail="Excel file not found on disk")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "active_job": job_store.active_job_id}
