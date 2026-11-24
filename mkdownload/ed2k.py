"""eD2k through Kelpie, a Go engine that runs as a child process and speaks JSON lines on stdin/stdout.

The control module in kelpie/ is Kelpie's own (MIT, v0.2.0). Kelpie keeps its resume state in /data/ed2k/state, so a
paused or interrupted transfer continues where it was.
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

from .errors import JobError
from .files import Disk
from .jobs import Context
from .kelpie import PROXY_SCHEMES, Error, ErrorCode, Kelpie, Link, Network, Settings as KelpieSettings
from .media import safe_name
from .settings import Settings
from .store import Job

log = logging.getLogger("mk-download.ed2k")

BUNDLED_LISTS = Path(__file__).parent / "lists"
LIST_SOURCES = {
    "server.met": ("https://upd.emule-security.org/server.met", "https://shortypower.org/server.met"),
    "nodes.dat": ("https://upd.emule-security.org/nodes.dat",),
}
LIST_MAX_AGE = 7 * 86400


def parse_link(url: str) -> Link:
    try:
        return Link.parse(url)
    except Error as error:
        raise JobError(JobError.INVALID_REQUEST, error.message) from None


def to_job_error(error: Error) -> JobError:
    match error.code:
        case ErrorCode.INVALID_LINK:
            return JobError(JobError.INVALID_REQUEST, error.message)
        case ErrorCode.DISK_FULL:
            return JobError(JobError.QUOTA_EXCEEDED, "The disk is full.")
        case ErrorCode.START_FAILED | ErrorCode.ENGINE_EXITED | ErrorCode.OUTDATED | ErrorCode.INTERNAL:
            return JobError(JobError.INTERNAL, f"Kelpie: {error.message}")
        case _:
            return JobError(JobError.DOWNLOAD_FAILED, f"Kelpie: {error.message}")


class Ed2kEngine:
    def __init__(self, settings: Settings, disk: Disk, data: Path, executable: Path, port: int) -> None:
        self.settings = settings
        self.disk = disk
        self.folder = data / "ed2k"
        self.executable = executable
        self.port = port
        self.kelpie: Kelpie | None = None
        self.network: Network | None = None
        self.seeding: dict[str, asyncio.Task] = {}

    def list_path(self, name: str) -> Path:
        path = self.folder / "lists" / name
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BUNDLED_LISTS / name, path)
        return path

    def build_settings(self) -> KelpieSettings:
        proxy = self.settings["proxy_url"]
        return KelpieSettings(
            port=self.port,
            enableKad=True,
            enableUpnp=False,
            serverLists=(self.list_path("server.met"),),
            nodeLists=(self.list_path("nodes.dat"),),
            downloadRateLimit=int(self.settings["speed_limit_kbps"]) * 1024,
            # Kelpie refuses any proxy but SOCKS5; an HTTP proxy is for the video sites only.
            proxy=proxy if proxy.split("://")[0] in PROXY_SCHEMES else "",
        )

    def ensure(self) -> Kelpie:
        if not self.settings["ed2k_enabled"]:
            raise JobError(JobError.ENGINE_DISABLED, "eD2k is turned off on the settings page.")
        if not self.executable.is_file():
            raise JobError(JobError.ENGINE_DISABLED, f"The Kelpie engine is not installed at {self.executable}.")
        if self.kelpie is None:
            (self.folder / "state").mkdir(parents=True, exist_ok=True)
            self.kelpie = Kelpie(lambda: self.executable, self.folder / "state", self.build_settings, self.on_network)
        else:
            self.kelpie.update()
        return self.kelpie

    def on_network(self, network: Network | None) -> None:
        self.network = network

    def describe_network(self) -> dict[str, Any] | None:
        network = self.network
        if network is None:
            return None
        return {
            "server_connected": network.isServerConnected,
            "high_id": network.isHighId,
            "kad_nodes": network.kadNodes,
            "kad_firewalled": network.isKadFirewalled,
        }

    def probe(self, url: str) -> dict[str, Any]:
        link = parse_link(url)
        return {
            "type": "ed2k",
            "name": link.name,
            "size": link.size,
            "hash": link.hash,
            "network": self.describe_network(),
            "note": "Source counts are known only once a download runs; see get_job.",
        }

    async def run(self, job: Job, ctx: Context) -> list[Path]:
        link = parse_link(job.url)
        kelpie = self.ensure()
        name = safe_name(job.params.get("name") or link.name)
        file = ctx.folder / name
        existing = file.stat().st_size if file.is_file() else 0
        self.disk.check_room(max(0, link.size - existing))
        ctx.report(title=link.name, total_bytes=link.size)
        checked = 0.0
        try:
            async with kelpie.runDownload(link, file) as run:
                async for progress in run:
                    now = time.monotonic()
                    if now - checked > 5:
                        checked = now
                        self.disk.check_room(0)
                    rate = progress.downloadRate
                    ctx.report(
                        percent=round(min(progress.received / max(progress.size, 1) * 100, 99.9), 1),
                        speed=rate,
                        eta=int((progress.size - progress.received) / rate) if rate else None,
                        downloaded_bytes=progress.received,
                        total_bytes=progress.size,
                        peers=progress.peers,
                        active_peers=progress.activePeers,
                        network=self.describe_network(),
                    )
        except Error as error:
            raise to_job_error(error) from None
        self.start_seeding(job, link, file)
        return [file]

    def start_seeding(self, job: Job, link: Link, file: Path) -> None:
        ratio = float(self.settings["seed_ratio"])
        minutes = int(self.settings["seed_minutes"])
        if (ratio <= 0 and minutes <= 0) or self.kelpie is None:
            return
        kelpie = self.kelpie

        async def seed() -> None:
            started = time.monotonic()
            try:
                async with kelpie.runSeed(link, file) as run:
                    async for progress in run:
                        if ratio > 0 and progress.uploaded >= ratio * link.size:
                            return
                        if minutes > 0 and time.monotonic() - started >= minutes * 60:
                            return
            except Error as error:
                log.info("job %s: seeding ended: %s", job.id, error.message)
            finally:
                self.seeding.pop(job.id, None)

        self.seeding[job.id] = asyncio.create_task(seed())

    async def discard(self, job: Job) -> None:
        task = self.seeding.pop(job.id, None)
        if task is not None:
            task.cancel()
        if self.kelpie is None:
            return
        try:
            await self.kelpie.remove(parse_link(job.url).hash)
        except (Error, JobError) as error:
            log.info("job %s: could not drop Kelpie's state: %s", job.id, error)

    async def refresh_lists(self) -> None:
        """Server and node lists go stale; refresh them weekly, keeping the bundled copy when a fetch fails."""
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for name, sources in LIST_SOURCES.items():
                path = self.list_path(name)
                if time.time() - path.stat().st_mtime < LIST_MAX_AGE and path.stat().st_size != (BUNDLED_LISTS / name).stat().st_size:
                    continue
                for url in sources:
                    try:
                        response = await client.get(url)
                    except httpx.HTTPError:
                        continue
                    if response.status_code == 200 and is_valid_list(name, response.content):
                        temporary = path.with_suffix(".download")
                        temporary.write_bytes(response.content)
                        temporary.replace(path)
                        break

    async def close(self) -> None:
        for task in list(self.seeding.values()):
            task.cancel()
        if self.kelpie is not None:
            await self.kelpie.close()
            self.kelpie = None


def is_valid_list(name: str, data: bytes) -> bool:
    if name == "server.met":
        return len(data) > 5 and data[0] in (0xE0, 0x0E, 0x0F)
    return len(data) > 40
