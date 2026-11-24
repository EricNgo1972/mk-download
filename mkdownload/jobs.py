"""The job manager: queue, concurrency slots, pause/resume/stop/cancel, and the status changes that feed the outbox.

Job states: queued -> waiting (scheduled live only) -> downloading or recording -> finalizing -> completed;
or paused, failed, cancelled.
"""

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import files
from .errors import JobError
from .settings import Settings
from .store import ACTIVE_STATES, FINAL_STATES, Job, Store
from .webhooks import Outbox

log = logging.getLogger("mk-download.jobs")

PROGRESS_SAVE_INTERVAL = 5
PROGRESS_STEPS = (25, 50, 75)


@dataclass
class Context:
    """What an engine gets for one run. Thread-safe: engines running in threads report through the loop."""

    job: Job
    folder: Path
    loop: asyncio.AbstractEventLoop
    on_progress: Callable[[dict[str, Any]], None]
    on_status: Callable[[str], None]
    # Set on pause, cancel and shutdown: stop as soon as possible, keep partial files.
    halt: threading.Event = field(default_factory=threading.Event)
    # Set by stop: a live recording ends and keeps what it has.
    stop: threading.Event = field(default_factory=threading.Event)
    reason: str = ""

    def report(self, **values: Any) -> None:
        self.loop.call_soon_threadsafe(self.on_progress, values)

    def set_status(self, status: str) -> None:
        self.loop.call_soon_threadsafe(self.on_status, status)


class Engine(Protocol):
    async def run(self, job: Job, ctx: Context) -> list[Path]: ...

    async def discard(self, job: Job) -> None: ...


@dataclass
class Running:
    job: Job
    ctx: Context
    task: asyncio.Task
    saved_at: float = 0.0


def new_id() -> str:
    return time.strftime("%Y%m%d-") + secrets.token_hex(4)


class Jobs:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        downloads: Path,
        engines: dict[str, Engine],
        outbox: Outbox,
    ) -> None:
        self.store = store
        self.settings = settings
        self.downloads = downloads
        self.engines = engines
        self.outbox = outbox
        self.running: dict[str, Running] = {}
        self.wake = asyncio.Event()
        self.is_closing = False

    # queries

    def job_by_id(self, job_id: str) -> Job:
        if job_id in self.running:
            return self.running[job_id].job
        job = self.store.job_by_id(job_id)
        if job is None:
            raise JobError(JobError.NOT_FOUND, f"No job {job_id}.")
        return job

    def by_status(self, status: str | None = None) -> list[Job]:
        jobs = self.store.jobs((status,) if status else ())
        return [self.running[job.id].job if job.id in self.running else job for job in jobs]

    def busy_folders(self) -> set[str]:
        busy = set(self.running)
        busy.update(job.id for job in self.store.jobs(("queued", "paused", *ACTIVE_STATES), limit=100000))
        return busy

    def folder_of(self, job: Job) -> Path:
        return self.downloads / job.id

    # commands

    def add(
        self,
        kind: str,
        url: str,
        params: dict[str, Any],
        *,
        is_live: bool = False,
        callback_url: str | None = None,
        callback_ref: str | None = None,
        notify_progress: bool = False,
        keep: bool = False,
    ) -> Job:
        if self.store.count_jobs("queued") >= self.settings["max_queue"]:
            raise JobError(JobError.QUEUE_FULL, f"The queue is full ({self.settings['max_queue']} jobs waiting).")
        job = Job(
            id=new_id(),
            kind=kind,
            url=url,
            params=params,
            status="queued",
            is_live=is_live,
            callback_url=callback_url,
            callback_ref=callback_ref,
            notify_progress=notify_progress,
            keep=keep,
            created=time.time(),
        )
        self.folder_of(job).mkdir(parents=True, exist_ok=True)
        self.store.add_job(job)
        self.wake.set()
        return job

    async def pause(self, job_id: str) -> Job:
        job = self.job_by_id(job_id)
        if job.id in self.running:
            await self.interrupt(job.id, "pause")
            return self.job_by_id(job_id)
        if job.status != "queued":
            raise JobError(JobError.INVALID_REQUEST, f"Job {job_id} is {job.status}; only queued or running jobs pause.")
        job.status = "paused"
        self.store.save_job(job)
        return job

    def resume(self, job_id: str) -> Job:
        job = self.job_by_id(job_id)
        if job.status not in ("paused", "failed"):
            raise JobError(JobError.INVALID_REQUEST, f"Job {job_id} is {job.status}; only paused or failed jobs resume.")
        job.status = "queued"
        job.error_code = job.error_message = None
        job.finished = None
        self.store.save_job(job)
        self.wake.set()
        return job

    async def stop(self, job_id: str) -> Job:
        job = self.job_by_id(job_id)
        if not job.is_live:
            raise JobError(JobError.NOT_LIVE, "stop ends live recordings; use pause or cancel for other jobs.")
        if job.status in FINAL_STATES:
            raise JobError(JobError.INVALID_REQUEST, f"Job {job_id} is already {job.status}.")
        running = self.running.get(job.id)
        if running is not None:
            running.ctx.stop.set()
            await asyncio.wait({running.task})
            return self.job_by_id(job_id)
        # Queued or paused with segments already on disk: finish them without taking a slot.
        await asyncio.wait({self.start(job, finalize_only=True)})
        return self.job_by_id(job_id)

    async def cancel(self, job_id: str) -> Job:
        job = self.job_by_id(job_id)
        if job.status in FINAL_STATES:
            raise JobError(JobError.INVALID_REQUEST, f"Job {job_id} is already {job.status}.")
        if job.id in self.running:
            await self.interrupt(job.id, "cancel")
        job = self.store.job_by_id(job_id)
        await self.engines[job.kind].discard(job)
        files.delete_folder(self.folder_of(job))
        self.store.delete_files_under(job.id)
        job.status = "cancelled"
        job.files = []
        job.finished = time.time()
        self.store.save_job(job, self.outbox.build_event(job, "job.cancelled"))
        self.outbox.wake.set()
        return job

    async def interrupt(self, job_id: str, reason: str) -> None:
        running = self.running[job_id]
        running.ctx.reason = reason
        running.ctx.halt.set()
        running.task.cancel()
        await asyncio.wait({running.task})

    # scheduling

    def recover(self) -> None:
        """After a restart, jobs that were running go back to the queue; engines resume from partial files."""
        for job in self.store.jobs(ACTIVE_STATES, limit=100000):
            job.status = "queued"
            self.store.save_job(job)

    async def run(self) -> None:
        while not self.is_closing:
            self.start_eligible()
            self.wake.clear()
            try:
                async with asyncio.timeout(2):
                    await self.wake.wait()
            except TimeoutError:
                pass

    def start_eligible(self) -> None:
        live = sum(1 for r in self.running.values() if r.job.is_live)
        normal = len(self.running) - live
        queued = sorted(self.store.jobs(("queued",), limit=100000), key=lambda job: job.created)
        for job in queued:
            if job.is_live and live < self.settings["max_live"]:
                live += 1
            elif not job.is_live and normal < self.settings["max_downloads"]:
                normal += 1
            else:
                continue
            self.start(job)

    def start(self, job: Job, finalize_only: bool = False) -> asyncio.Task:
        """Registers the job as running before its task gets to run, so the next scheduling pass skips it."""
        ctx = Context(
            job=job,
            folder=self.folder_of(job),
            loop=asyncio.get_running_loop(),
            on_progress=lambda values: self.on_progress(job.id, values),
            on_status=lambda status: self.on_status(job.id, status),
        )
        ctx.folder.mkdir(parents=True, exist_ok=True)
        if finalize_only:
            ctx.stop.set()
        task = asyncio.create_task(self.run_job(job, ctx), name=f"job-{job.id}")
        self.running[job.id] = Running(job, ctx, task)
        return task

    async def run_job(self, job: Job, ctx: Context) -> None:
        job.status = "recording" if job.is_live else "downloading"
        job.started = job.started or time.time()
        job.error_code = job.error_message = None
        self.store.save_job(job, self.build_progress_event(job, "job.started"))
        try:
            outputs = await self.engines[job.kind].run(job, ctx)
        except asyncio.CancelledError:
            if ctx.reason == "pause":
                job.status = "paused"
                self.store.save_job(job)
            elif ctx.reason in ("shutdown", ""):
                job.status = "queued"
                self.store.save_job(job)
            # cancel() writes the final state itself, after deleting the files.
        except JobError as error:
            self.fail(job, error.code, error.message)
        except Exception as error:
            log.exception("job %s crashed", job.id)
            self.fail(job, JobError.INTERNAL, f"Unexpected error: {type(error).__name__}: {error}")
        else:
            self.complete(job, outputs)
        finally:
            self.running.pop(job.id, None)
            self.wake.set()

    def complete(self, job: Job, outputs: list[Path]) -> None:
        paths = [files.to_relative(self.downloads, file) for file in outputs if file.is_file()]
        for path in paths:
            self.store.add_file(path, job.id, job.keep)
        job.files = paths
        job.status = "completed"
        job.finished = time.time()
        job.progress["percent"] = 100.0
        job.progress.pop("speed", None)
        job.progress.pop("eta", None)
        self.store.save_job(job, self.outbox.build_event(job, "job.completed"))
        self.outbox.wake.set()
        log.info("job %s completed: %s", job.id, ", ".join(paths) or "no files")

    def fail(self, job: Job, code: str, message: str) -> None:
        job.status = "failed"
        job.error_code = code
        job.error_message = message
        job.finished = time.time()
        job.progress.pop("speed", None)
        job.progress.pop("eta", None)
        self.store.save_job(job, self.outbox.build_event(job, "job.failed"))
        self.outbox.wake.set()
        log.info("job %s failed: %s: %s", job.id, code, message)

    def on_status(self, job_id: str, status: str) -> None:
        running = self.running.get(job_id)
        if running is None or running.job.status == status:
            return
        running.job.status = status
        self.store.save_job(running.job)

    def on_progress(self, job_id: str, values: dict[str, Any]) -> None:
        running = self.running.get(job_id)
        if running is None:
            return
        job = running.job
        before = job.progress.get("percent") or 0
        job.progress.update(values)
        after = job.progress.get("percent") or 0
        event = None
        if job.notify_progress and any(before < step <= after for step in PROGRESS_STEPS):
            event = self.build_progress_event(job, "job.progress")
        now = time.monotonic()
        if event is not None:
            self.store.save_job(job, event)
            self.outbox.wake.set()
            running.saved_at = now
        elif now - running.saved_at > PROGRESS_SAVE_INTERVAL:
            self.store.save_progress(job)
            running.saved_at = now

    def build_progress_event(self, job: Job, type: str) -> dict[str, Any] | None:
        return self.outbox.build_event(job, type) if job.notify_progress else None

    async def close(self) -> None:
        """On SIGTERM: interrupt running jobs so they resume after the restart."""
        self.is_closing = True
        for job_id in list(self.running):
            if job_id in self.running:
                await self.interrupt(job_id, "shutdown")
