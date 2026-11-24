"""Builds the service's parts from the environment and runs the background loops."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from . import files, links
from .clip import ClipEngine
from .ed2k import Ed2kEngine
from .env import Env
from .errors import JobError
from .files import Disk
from .jobs import Jobs
from .settings import Settings
from .sites import Cookies
from .store import Job, Store
from .torrent import TorrentEngine
from .video import VideoEngine
from .webhooks import Outbox

log = logging.getLogger("mk-download")

CLEANUP_EVERY = 3600
KEEP_JOB_RECORDS = 30 * 86400


def to_iso(timestamp: float | None) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds") if timestamp else None


class Service:
    def __init__(self, env: Env) -> None:
        self.env = env
        self.store = Store(env.database)
        self.settings = Settings(self.store, proxy_default=env.proxy_url)
        self.disk = Disk(env.downloads, self.settings)
        self.cookies = Cookies(env.cookies, self.store)
        scratch = env.state / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        self.video = VideoEngine(self.settings, self.disk, self.cookies, scratch)
        self.torrent = TorrentEngine(self.settings, self.disk, env.state, env.bt_port)
        self.ed2k = Ed2kEngine(self.settings, self.disk, env.data, env.kelpie_path, env.ed2k_port)
        self.clip = ClipEngine(env.downloads, self.settings)
        self.outbox = Outbox(self.store, env.webhook_secret, self.callback_url, self.describe_files)
        self.jobs = Jobs(
            self.store,
            self.settings,
            env.downloads,
            {"video": self.video, "torrent": self.torrent, "ed2k": self.ed2k, "clip": self.clip},
            self.outbox,
        )
        self.tasks: list[asyncio.Task] = []

    # public URL, for links in tool replies and webhooks

    @property
    def base_url(self) -> str:
        return self.env.public_url or self.store.load_value("public_url") or ""

    def learn_base_url(self, url: str) -> None:
        if not self.env.public_url and url != self.store.load_value("public_url"):
            self.store.save_value("public_url", url)

    def build_link(self, path: str, expires_in: float | None = None) -> dict[str, Any]:
        file = files.resolve(self.env.downloads, path)
        if not file.is_file():
            raise JobError(JobError.NOT_FOUND, f"No file {path}.")
        relative = files.to_relative(self.env.downloads, file)
        maximum = self.settings["link_max_days"] * 86400
        seconds = min(float(expires_in) if expires_in else self.settings["link_hours"] * 3600, maximum)
        if seconds <= 0:
            raise JobError(JobError.INVALID_REQUEST, "expires_in must be positive.")
        expires = time.time() + seconds
        # A link never outlives its file.
        deletes = files.deletes_at(self.store, self.settings, self.env.downloads, relative)
        if deletes is not None:
            expires = min(expires, deletes)
        if not self.base_url:
            raise JobError(JobError.INTERNAL, "The service doesn't know its public URL yet; set PUBLIC_URL.")
        return {
            "path": relative,
            "url": links.build_link(self.base_url, self.env.link_secret, relative, int(expires)),
            "expires_at": to_iso(expires),
        }

    # descriptions shared by tools and webhooks

    def callback_url(self, job: Job) -> str | None:
        return job.callback_url or self.settings["default_callback_url"] or None

    def describe_files(self, job: Job) -> list[dict[str, Any]]:
        described = []
        for path in job.files:
            file = self.env.downloads / path
            if not file.is_file():
                continue
            item = {"path": path, "size": file.stat().st_size, "link": None, "link_expires_at": None}
            try:
                link = self.build_link(path)
                item["link"], item["link_expires_at"] = link["url"], link["expires_at"]
            except JobError:
                pass
            described.append(item)
        return described

    def describe_job(self, job: Job, with_links: bool = False) -> dict[str, Any]:
        progress = dict(job.progress)
        result = {
            "id": job.id,
            "kind": job.kind,
            "url": job.url,
            "status": job.status,
            "live": job.is_live,
            "progress": progress,
            "files": self.describe_files(job) if with_links else [
                {"path": path, "size": (self.env.downloads / path).stat().st_size}
                for path in job.files if (self.env.downloads / path).is_file()
            ],
            "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
            "callback_ref": job.callback_ref,
            "created": to_iso(job.created),
            "started": to_iso(job.started),
            "finished": to_iso(job.finished),
        }
        return result

    # background

    async def start(self) -> None:
        self.jobs.recover()
        self.tasks = [
            asyncio.create_task(self.jobs.run(), name="jobs"),
            asyncio.create_task(self.outbox.run(), name="webhooks"),
            asyncio.create_task(self.run_cleanup(), name="cleanup"),
            asyncio.create_task(self.refresh_ed2k_lists(), name="ed2k-lists"),
        ]

    async def run_cleanup(self) -> None:
        while True:
            try:
                count = files.delete_expired(self.env.downloads, self.store, self.settings, self.jobs.busy_folders())
                now = time.time()
                self.store.delete_old_events(now - KEEP_JOB_RECORDS)
                self.store.delete_old_jobs(now - KEEP_JOB_RECORDS)
                if count:
                    log.info("cleanup deleted %d expired files", count)
            except Exception:
                log.exception("cleanup failed")
            await asyncio.sleep(CLEANUP_EVERY)

    async def refresh_ed2k_lists(self) -> None:
        try:
            await self.ed2k.refresh_lists()
        except Exception as error:
            log.info("eD2k list refresh failed: %s", error)

    async def close(self) -> None:
        await self.jobs.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.ed2k.close()
        await self.torrent.close()
        self.video.close()


def versions() -> dict[str, str]:
    import os

    import curl_cffi
    import libtorrent
    import yt_dlp.version

    return {
        "yt-dlp": yt_dlp.version.__version__,
        "curl_cffi": curl_cffi.__version__,
        "libtorrent": libtorrent.__version__,
        "kelpie": os.environ.get("KELPIE_VERSION", "not installed"),
    }
