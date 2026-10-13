from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import parse_qs, urlparse

from redis.asyncio import Redis
from websockets.asyncio.server import ServerConnection, serve

from .status import report_status_payload


LOG = logging.getLogger(__name__)
SUCCESSFUL_CLOSE_CODE = 1000
UNSUCCESSFUL_REPORT_CLOSE_CODE = 1011
SUPPORTED_TARGET_STATUSES = {"completed", "reviewed"}


def _valid_results_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


async def _read_subscription(websocket: ServerConnection) -> tuple[str, str]:
    query = parse_qs(urlparse(websocket.request.path).query)
    results_id = (query.get("results_id") or [""])[0].strip()
    target_status = (query.get("status") or ["completed"])[0].strip().lower()
    if results_id:
        return results_id, target_status
    message = await asyncio.wait_for(websocket.recv(), timeout=30)
    if not isinstance(message, str):
        return "", target_status
    try:
        body = json.loads(message)
    except json.JSONDecodeError:
        return message.strip(), target_status
    if not isinstance(body, dict):
        return "", target_status
    return (
        str(body.get("results_id", "")).strip(),
        str(body.get("status", target_status)).strip().lower(),
    )


async def _read_results_id(websocket: ServerConnection) -> str:
    """Backward-compatible helper for callers that only need the report ID."""
    results_id, _ = await _read_subscription(websocket)
    return results_id


async def _send_error(websocket: ServerConnection, error: str) -> None:
    await websocket.send(json.dumps({"error": error}))
    await websocket.close(code=1008, reason=error)


async def _close_for_terminal_status(
    websocket: ServerConnection, status: str, target_status: str = "completed"
) -> bool:
    if status == target_status:
        await websocket.close(code=SUCCESSFUL_CLOSE_CODE, reason=f"Report {status}")
        return True
    if status in {"failed", "stopped"}:
        await websocket.close(code=UNSUCCESSFUL_REPORT_CLOSE_CODE, reason=f"Report {status}")
        return True
    return False


async def handle_connection(websocket: ServerConnection) -> None:
    try:
        results_id, target_status = await _read_subscription(websocket)
    except (asyncio.TimeoutError, ConnectionError):
        await websocket.close(code=1008, reason="results_id was not received")
        return
    if not _valid_results_id(results_id):
        await _send_error(websocket, "Invalid results_id")
        return
    if target_status not in SUPPORTED_TARGET_STATUSES:
        await _send_error(websocket, "status must be completed or reviewed")
        return

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    prefix = os.environ.get("CODEQC_REDIS_PREFIX", "codeqc").rstrip(":")
    client = Redis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()
    channel = f"{prefix}:updates:{results_id}"
    try:
        await pubsub.subscribe(channel)
        raw_job = await client.get(f"{prefix}:job:{results_id}")
        if raw_job is None:
            await _send_error(websocket, "Report not found")
            return
        job = json.loads(raw_job)
        await websocket.send(json.dumps(report_status_payload(job), ensure_ascii=False))
        if await _close_for_terminal_status(websocket, job.get("status", ""), target_status):
            return

        while True:
            update_task = asyncio.create_task(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
            )
            closed_task = asyncio.create_task(websocket.wait_closed())
            done, pending = await asyncio.wait(
                {update_task, closed_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if closed_task in done:
                return
            message = update_task.result()
            if message is None:
                continue
            job = json.loads(message["data"])
            if job.get("deleted"):
                await _send_error(websocket, "Report deleted")
                return
            await websocket.send(json.dumps(report_status_payload(job), ensure_ascii=False))
            if await _close_for_terminal_status(websocket, job.get("status", ""), target_status):
                return
    except json.JSONDecodeError:
        LOG.exception("Invalid report state for %s", results_id)
        await _send_error(websocket, "Invalid report state")
    finally:
        await pubsub.aclose()
        await client.aclose()


async def run() -> None:
    host = os.environ.get("CODEQC_WS_HOST", "0.0.0.0")
    port = int(os.environ.get("CODEQC_WS_PORT", "8081"))
    async with serve(handle_connection, host, port):
        LOG.info("AI CICD Security Guardrail WebSocket server listening on %s:%d", host, port)
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    asyncio.run(run())
