"""Completion notifications: outbox rows written with the job's status change, delivered by one worker with retries.

Delivery is at-least-once; callers deduplicate on event_id.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from .store import Job, Store

log = logging.getLogger("mk-download.webhooks")

# 30 s, 2 min, 10 min, 1 h, then every 6 h, for up to 24 h.
RETRY_DELAYS = (30, 120, 600, 3600)
RETRY_EVERY = 6 * 3600
GIVE_UP_AFTER = 24 * 3600
TIMEOUT = 15
PARALLEL = 4


def to_iso(timestamp: float | None) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds") if timestamp else None


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def next_delay(attempts: int) -> int:
    return RETRY_DELAYS[attempts - 1] if attempts <= len(RETRY_DELAYS) else RETRY_EVERY


class Outbox:
    def __init__(
        self,
        store: Store,
        secret: str,
        callback_url: Callable[[Job], str | None],
        describe_files: Callable[[Job], list[dict[str, Any]]],
    ) -> None:
        self.store = store
        self.secret = secret
        self.callback_url = callback_url
        self.describe_files = describe_files
        self.wake = asyncio.Event()

    def build_event(self, job: Job, type: str, extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """An outbox row for the job's callback, or None when the job has nowhere to send it."""
        url = self.callback_url(job)
        if not url or not self.secret:
            return None
        event_id = str(uuid.uuid4())
        now = time.time()
        body = {
            "event_id": event_id,
            "type": type,
            "job_id": job.id,
            "callback_ref": job.callback_ref,
            "status": job.status,
            "kind": job.kind,
            "url": job.url,
            "files": self.describe_files(job) if type == "job.completed" else [],
            "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
            "progress": job.progress if type in ("job.progress", "job.started") else None,
            "created": to_iso(job.created),
            "finished": to_iso(job.finished),
            "sent": to_iso(now),
        }
        return {
            "id": event_id,
            "job_id": job.id,
            "type": type,
            "url": url,
            "body": json.dumps(body, separators=(",", ":")),
            "next_at": now,
            "created": now,
        }

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as client:
            while True:
                due = self.store.due_events(time.time(), limit=PARALLEL * 5)
                if due:
                    queue = list(due)

                    async def drain(queue: list) -> None:
                        while queue:
                            await self.send(client, queue.pop(0))

                    await asyncio.gather(*(drain(queue) for _ in range(PARALLEL)))
                    continue
                self.wake.clear()
                try:
                    async with asyncio.timeout(5):
                        await self.wake.wait()
                except TimeoutError:
                    pass

    async def send(self, client: httpx.AsyncClient, event) -> None:
        body = event["body"].encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "mk-download-webhook/1",
            "X-MK-Event-Id": event["id"],
            "X-MK-Event-Type": event["type"],
            "X-MK-Signature": sign(self.secret, body),
        }
        error = None
        try:
            response = await client.post(event["url"], content=body, headers=headers)
            if 200 <= response.status_code < 300:
                self.store.save_event_result(
                    event["id"], status="delivered", attempts=event["attempts"] + 1, delivered=time.time(), last_error=None
                )
                return
            error = f"HTTP {response.status_code}"
        except httpx.HTTPError as failure:
            error = f"{type(failure).__name__}: {failure}"[:300]
        attempts = event["attempts"] + 1
        now = time.time()
        if now - event["created"] >= GIVE_UP_AFTER:
            log.warning("webhook %s for job %s undelivered after %d attempts: %s", event["id"], event["job_id"], attempts, error)
            self.store.save_event_result(event["id"], status="undelivered", attempts=attempts, last_error=error)
            return
        self.store.save_event_result(
            event["id"], attempts=attempts, last_error=error, next_at=now + next_delay(attempts)
        )

    def retry(self, event_id: str) -> None:
        """From the settings page: give an undelivered event another 24 h."""
        now = time.time()
        self.store.save_event_result(event_id, status="pending", next_at=now, created=now, attempts=0)
        self.wake.set()
