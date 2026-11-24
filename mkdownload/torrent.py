"""BitTorrent through libtorrent: magnet links, .torrent URLs and uploaded .torrent files.

One session for the process. A paused or interrupted torrent saves libtorrent's resume data under /data/state, so it
continues without rechecking. Seeding is off by default: a finished torrent leaves the session at once.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import libtorrent as lt

from .errors import JobError
from .files import Disk
from .jobs import Context
from .settings import Settings
from .store import Job

log = logging.getLogger("mk-download.torrent")

METADATA_TIMEOUT = 3600
PROBE_TIMEOUT = 60
MAX_TORRENT_FILE = 20 * 1024 * 1024


def is_torrent_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("magnet:") or (lowered.startswith(("http://", "https://")) and lowered.split("?")[0].endswith(".torrent"))


def parse_torrent(data: bytes) -> "lt.torrent_info":
    try:
        return lt.torrent_info(lt.bdecode(data))
    except (RuntimeError, TypeError, ValueError) as error:
        raise JobError(JobError.INVALID_REQUEST, f"Not a valid .torrent file: {error}") from None


def describe_torrent(info: "lt.torrent_info") -> dict[str, Any]:
    files = info.files()
    return {
        "type": "torrent",
        "name": info.name(),
        "size": info.total_size(),
        "info_hash": str(info.info_hashes().get_best()),
        "file_count": files.num_files(),
        "files": [
            {"index": index, "path": files.file_path(index), "size": files.file_size(index)}
            for index in range(min(files.num_files(), 1000))
        ],
    }


class TorrentEngine:
    def __init__(self, settings: Settings, disk: Disk, state: Path, port: int) -> None:
        self.settings = settings
        self.disk = disk
        self.state = state
        self.port = port
        self.session: lt.session | None = None
        self.handles: dict[str, lt.torrent_handle] = {}
        self.seeding: dict[str, asyncio.Task] = {}
        self.resume_alerts: list[Any] = []

    def build_settings(self) -> dict[str, Any]:
        return {
            "listen_interfaces": f"0.0.0.0:{self.port},[::]:{self.port}",
            "user_agent": "mk-download/1.0 libtorrent/" + lt.__version__,
            "alert_mask": lt.alert_category.error | lt.alert_category.storage | lt.alert_category.status,
            "enable_dht": True,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "connections_limit": int(self.settings["max_peers"]),
            "download_rate_limit": int(self.settings["speed_limit_kbps"]) * 1024,
        }

    def ensure_session(self) -> lt.session:
        if not self.settings["torrent_enabled"]:
            raise JobError(JobError.ENGINE_DISABLED, "BitTorrent is turned off on the settings page.")
        if self.session is None:
            self.session = lt.session(self.build_settings())
        else:
            self.session.apply_settings(self.build_settings())
        return self.session

    # probe

    async def probe(self, url: str) -> dict[str, Any]:
        if not url.lower().startswith("magnet:"):
            return describe_torrent(parse_torrent(await fetch_torrent(url, self.settings["proxy_url"])))
        session = self.ensure_session()
        params = lt.parse_magnet_uri(url)
        params.save_path = str(self.state / "probe")
        params.flags |= lt.torrent_flags.upload_mode
        handle = session.add_torrent(params)
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                while not handle.status().has_metadata:
                    await asyncio.sleep(1)
            return describe_torrent(handle.torrent_file())
        except TimeoutError:
            raise JobError(JobError.SOURCE_UNAVAILABLE, f"No peer sent the torrent's metadata within {PROBE_TIMEOUT} s.") from None
        finally:
            session.remove_torrent(handle, lt.session.delete_files)

    # jobs

    async def run(self, job: Job, ctx: Context) -> list[Path]:
        session = self.ensure_session()
        params = await self.build_params(job, ctx.folder)
        handle = session.add_torrent(params)
        self.handles[job.id] = handle
        wanted: list[int] | None = job.params.get("files")
        has_metadata = False
        started = time.monotonic()
        checked = 0.0
        try:
            while True:
                status = handle.status()
                if status.errc.value():
                    raise JobError(JobError.DOWNLOAD_FAILED, f"Torrent error: {status.errc.message()}")
                if not status.has_metadata:
                    if time.monotonic() - started > METADATA_TIMEOUT:
                        raise JobError(JobError.SOURCE_UNAVAILABLE, "No peer sent the torrent's metadata within an hour.")
                    ctx.report(state="fetching metadata", peers=status.num_peers)
                    await asyncio.sleep(1)
                    continue
                if not has_metadata:
                    has_metadata = True
                    info = handle.torrent_file()
                    self.save_torrent(job, info)
                    if wanted:
                        count = info.files().num_files()
                        if any(index < 0 or index >= count for index in wanted):
                            raise JobError(JobError.INVALID_REQUEST, f"files must be indexes from 0 to {count - 1}.")
                        handle.prioritize_files([4 if index in wanted else 0 for index in range(count)])
                        await asyncio.sleep(0.5)
                        status = handle.status()
                    self.disk.check_room(max(0, status.total_wanted - status.total_wanted_done))
                    ctx.report(title=info.name(), total_bytes=status.total_wanted)
                now = time.monotonic()
                if now - checked > 5:
                    checked = now
                    self.disk.check_room(0)
                total = status.total_wanted or 1
                done = status.total_wanted_done
                rate = status.download_payload_rate
                ctx.report(
                    state=str(status.state).split(".")[-1],
                    percent=round(min(done / total * 100, 99.9), 1),
                    speed=rate,
                    eta=int((total - done) / rate) if rate else None,
                    downloaded_bytes=done,
                    total_bytes=status.total_wanted,
                    peers=status.num_peers,
                    seeds=status.num_seeds,
                )
                if status.is_finished and status.total_wanted_done >= status.total_wanted:
                    break
                await asyncio.sleep(1)
        except BaseException:
            # Paused, cancelled, failed or shutting down: keep resume data, let go of the files.
            await self.save_resume(job, handle)
            self.release(job.id)
            raise
        selected, unselected = self.split_files(handle, ctx.folder)
        if not self.start_seeding(job, handle):  # released: the placeholders are ours to delete
            for path in unselected:
                path.unlink(missing_ok=True)
            for leftover in ctx.folder.glob(".*.parts"):
                leftover.unlink(missing_ok=True)
        self.forget_state(job)
        return selected

    async def build_params(self, job: Job, folder: Path) -> "lt.add_torrent_params":
        resume = self.state / f"{job.id}.resume"
        torrent = self.state / f"{job.id}.torrent"
        if resume.is_file():
            params = lt.read_resume_data(resume.read_bytes())
        elif torrent.is_file():
            params = lt.add_torrent_params()
            params.ti = parse_torrent(torrent.read_bytes())
        elif job.url.lower().startswith("magnet:"):
            params = lt.parse_magnet_uri(job.url)
        else:
            data = await fetch_torrent(job.url, self.settings["proxy_url"])
            torrent.write_bytes(data)
            params = lt.add_torrent_params()
            params.ti = parse_torrent(data)
        params.save_path = str(folder)
        return params

    def save_torrent(self, job: Job, info: "lt.torrent_info") -> None:
        torrent = self.state / f"{job.id}.torrent"
        if not torrent.is_file():
            torrent.write_bytes(lt.bencode(lt.create_torrent(info).generate()))

    async def save_resume(self, job: Job, handle: "lt.torrent_handle") -> None:
        if self.session is None or not handle.is_valid() or not handle.status().has_metadata:
            return
        handle.save_resume_data(lt.torrent_handle.save_info_dict)
        try:
            async with asyncio.timeout(10):
                while True:
                    for alert in self.session.pop_alerts():
                        if isinstance(alert, lt.save_resume_data_alert) and alert.handle == handle:
                            (self.state / f"{job.id}.resume").write_bytes(lt.write_resume_data_buf(alert.params))
                            return
                        if isinstance(alert, lt.save_resume_data_failed_alert) and alert.handle == handle:
                            return
                    await asyncio.sleep(0.2)
        except TimeoutError:
            log.warning("job %s: no resume data within 10 s", job.id)

    def split_files(self, handle: "lt.torrent_handle", folder: Path) -> tuple[list[Path], list[Path]]:
        """(selected files, unselected ones). libtorrent leaves sparse placeholders for unselected files whose
        pieces straddle a selected file's edge."""
        files = handle.torrent_file().files()
        priorities = handle.get_file_priorities()
        selected, unselected = [], []
        for index in range(files.num_files()):
            if files.file_flags(index) & lt.file_storage.flag_pad_file:
                continue
            (selected if priorities[index] > 0 else unselected).append(folder / files.file_path(index))
        return selected, unselected

    def start_seeding(self, job: Job, handle: "lt.torrent_handle") -> bool:
        ratio = float(self.settings["seed_ratio"])
        minutes = int(self.settings["seed_minutes"])
        if ratio <= 0 and minutes <= 0:
            self.release(job.id)
            return False

        async def seed() -> None:
            started = time.monotonic()
            try:
                while True:
                    await asyncio.sleep(30)
                    status = handle.status()
                    uploaded = status.all_time_upload
                    if ratio > 0 and uploaded >= ratio * max(status.total_wanted, 1):
                        return
                    if minutes > 0 and time.monotonic() - started >= minutes * 60:
                        return
            finally:
                self.release(job.id)
                self.seeding.pop(job.id, None)

        self.seeding[job.id] = asyncio.create_task(seed())
        return True

    def release(self, job_id: str) -> None:
        handle = self.handles.pop(job_id, None)
        if handle is not None and self.session is not None and handle.is_valid():
            self.session.remove_torrent(handle)

    def forget_state(self, job: Job) -> None:
        (self.state / f"{job.id}.resume").unlink(missing_ok=True)
        (self.state / f"{job.id}.torrent").unlink(missing_ok=True)

    async def discard(self, job: Job) -> None:
        task = self.seeding.pop(job.id, None)
        if task is not None:
            task.cancel()
        self.release(job.id)
        self.forget_state(job)

    async def close(self) -> None:
        for task in list(self.seeding.values()):
            task.cancel()
        self.session = None


async def fetch_torrent(url: str, proxy: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=proxy or None) as client:
            response = await client.get(url)
    except httpx.HTTPError as error:
        raise JobError(JobError.SOURCE_UNAVAILABLE, f"Could not fetch the .torrent file: {error}") from None
    if response.status_code != 200:
        raise JobError(JobError.SOURCE_UNAVAILABLE, f"Fetching the .torrent file answered HTTP {response.status_code}.")
    if len(response.content) > MAX_TORRENT_FILE:
        raise JobError(JobError.INVALID_REQUEST, "The .torrent file is larger than 20 MB.")
    return response.content
