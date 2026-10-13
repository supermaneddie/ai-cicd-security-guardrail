from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redis import Redis
from redis.exceptions import WatchError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RedisJobStore:
    """Redis-backed AI CICD Security Guardrail job metadata shared by web and RQ workers."""

    def __init__(self, client: Redis, prefix: str = "codeqc"):
        self.client = client
        self.prefix = prefix.rstrip(":")
        self.index_key = f"{self.prefix}:jobs"

    def _key(self, results_id: str) -> str:
        return f"{self.prefix}:job:{results_id}"

    def _channel(self, results_id: str) -> str:
        return f"{self.prefix}:updates:{results_id}"

    def create(
        self,
        results_id: str,
        model_name: str,
        review_type: str,
        files: list[str] | None = None,
        owner_pid: int | None = None,
        user_prompt: str = "",
    ) -> dict[str, Any]:
        now = _now()
        job = {
            "results_id": results_id,
            "status": "queued",
            "progress": "Upload accepted; waiting for an RQ worker.",
            "model_name": model_name,
            "type": review_type,
            "files": files or [],
            "user_prompt": user_prompt,
            "exception": "",
            "trace": [],
            "checklist": [],
            "workspace_available": True,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
        }
        with self.client.pipeline(transaction=True) as pipe:
            pipe.set(self._key(results_id), json.dumps(job, ensure_ascii=False))
            pipe.zadd(self.index_key, {results_id: datetime.now(timezone.utc).timestamp()})
            pipe.publish(self._channel(results_id), json.dumps(job, ensure_ascii=False))
            pipe.execute()
        return job

    def get(self, results_id: str) -> dict[str, Any] | None:
        value = self.client.get(self._key(results_id))
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None

    def update(self, results_id: str, **changes: Any) -> dict[str, Any]:
        key = self._key(results_id)
        while True:
            with self.client.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    value = pipe.get(key)
                    if value is None:
                        raise KeyError(results_id)
                    job = json.loads(value)
                    job.update(changes)
                    job["updated_at"] = _now()
                    pipe.multi()
                    pipe.set(key, json.dumps(job, ensure_ascii=False))
                    pipe.publish(self._channel(results_id), json.dumps(job, ensure_ascii=False))
                    pipe.execute()
                    return job
                except WatchError:
                    continue

    def delete(self, results_id: str) -> bool:
        with self.client.pipeline(transaction=True) as pipe:
            pipe.delete(self._key(results_id))
            pipe.zrem(self.index_key, results_id)
            pipe.publish(self._channel(results_id), json.dumps({"results_id": results_id, "deleted": True}))
            deleted, _, _ = pipe.execute()
        return bool(deleted)

    def list_all(self) -> list[dict[str, Any]]:
        identifiers = self.client.zrevrange(self.index_key, 0, -1)
        if not identifiers:
            return []
        values = self.client.mget([self._key(identifier) for identifier in identifiers])
        jobs: list[dict[str, Any]] = []
        stale: list[str] = []
        for identifier, value in zip(identifiers, values):
            if value is None:
                stale.append(identifier)
                continue
            try:
                jobs.append(json.loads(value))
            except (TypeError, json.JSONDecodeError):
                stale.append(identifier)
        if stale:
            self.client.zrem(self.index_key, *stale)
        return jobs

    def import_json_directory(self, directory: Path) -> int:
        """Import legacy disk metadata without overwriting Redis jobs."""
        imported = 0
        if not directory.is_dir():
            return imported
        for path in directory.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                results_id = str(job["results_id"])
                created = datetime.fromisoformat(str(job.get("created_at") or _now()).replace("Z", "+00:00"))
            except (OSError, KeyError, ValueError, json.JSONDecodeError):
                continue
            if self.client.set(self._key(results_id), json.dumps(job, ensure_ascii=False), nx=True):
                self.client.zadd(self.index_key, {results_id: created.timestamp()})
                imported += 1
            migrated_path = path.with_suffix(path.suffix + ".migrated")
            try:
                path.replace(migrated_path)
            except OSError:
                pass
        return imported
