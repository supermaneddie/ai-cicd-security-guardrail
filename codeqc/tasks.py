from __future__ import annotations

import asyncio
import logging
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path

from redis import Redis

from .agent import run_review_async
from .config import load_config
from .redis_jobs import RedisJobStore
from .report import write_report


LOG = logging.getLogger(__name__)


def execute_review_task(
    results_id: str,
    config_path: str,
    redis_url: str,
    redis_prefix: str,
    report_dir: str,
    workspace: str,
    model_name: str,
    review_type: str,
    user_prompt: str,
    files: list[str],
) -> None:
    """RQ entry point for a single AI CICD Security Guardrail review."""
    settings = load_config(config_path)
    client = Redis.from_url(redis_url, decode_responses=True)
    jobs = RedisJobStore(client, redis_prefix)

    def progress(message: str) -> None:
        jobs.update(results_id, progress=message)

    def trace(message: str) -> None:
        job = jobs.get(results_id) or {}
        entries = list(job.get("trace") or [])
        entries.append({"time": datetime.now(timezone.utc).isoformat(), "message": message})
        jobs.update(results_id, trace=entries[-200:])

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        jobs.update(
            results_id,
            status="running",
            started_at=started_at,
            progress="Starting the asynchronous LangGraph review.",
        )
        content = asyncio.run(
            run_review_async(
                settings,
                model_name,
                review_type,
                user_prompt,
                Path(workspace),
                progress,
                trace,
            )
        )
        job = jobs.get(results_id) or {}
        finished_at = datetime.now(timezone.utc).isoformat()
        write_report(
            Path(report_dir),
            results_id,
            content,
            {
                "model_name": model_name,
                "type": review_type,
                "user_prompt": user_prompt,
                "files": files,
                "created_at": job.get("created_at"),
                "started_at": started_at,
                "finished_at": finished_at,
                "trace": job.get("trace") or [],
                "workspace_available": False,
            },
        )
        jobs.update(results_id, status="completed", finished_at=finished_at, progress="Review completed.")
    except Exception as exc:
        LOG.exception("Code review failed for %s", results_id)
        jobs.update(
            results_id,
            status="failed",
            finished_at=datetime.now(timezone.utc).isoformat(),
            progress=f"Review failed ({type(exc).__name__}). See the exception below.",
            exception=traceback.format_exc(),
        )
        raise
    finally:
        shutil.rmtree(Path(workspace), ignore_errors=True)
        try:
            jobs.update(results_id, workspace_available=False)
        except KeyError:
            pass
        client.close()


def review_job_failed(job, connection, exc_type, exc_value, traceback_object, *args, **kwargs) -> None:
    exception_name = getattr(exc_type, "__name__", str(exc_type))
    _cleanup_terminated_rq_job(job, f"{exception_name}: {exc_value}")


def review_job_stopped(job, connection, *args, **kwargs) -> None:
    _cleanup_terminated_rq_job(job, "", status="stopped", progress="Review stopped by user.")


def _cleanup_terminated_rq_job(
    job,
    exception: str,
    status: str = "failed",
    progress: str = "Review terminated before completion.",
) -> None:
    try:
        results_id, _config, redis_url, redis_prefix, _reports, workspace = job.args[:6]
    except (TypeError, ValueError):
        return
    shutil.rmtree(Path(workspace), ignore_errors=True)
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        jobs = RedisJobStore(client, redis_prefix)
        current = jobs.get(results_id)
        if current is not None:
            jobs.update(
                results_id,
                status=status,
                finished_at=current.get("finished_at") or datetime.now(timezone.utc).isoformat(),
                progress=progress,
                exception=current.get("exception") or exception,
                workspace_available=False,
            )
    finally:
        client.close()
