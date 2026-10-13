from __future__ import annotations

from typing import Any


TERMINAL_STATUSES = {"completed", "reviewed", "failed", "stopped"}


def report_status_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Return the public status representation shared by HTTP and WebSocket APIs."""
    return {
        "results_id": job.get("results_id", ""),
        "status": job.get("status", "unknown"),
        "progress": job.get("progress", ""),
        "exception": job.get("exception", ""),
        "trace": job.get("trace", []),
        "checklist": job.get("checklist", []),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "updated_at": job.get("updated_at"),
    }
