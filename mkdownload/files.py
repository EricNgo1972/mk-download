"""The downloads folder: safe path resolution, disk usage and quota checks, listing and hourly cleanup."""

import os
import shutil
import time
from pathlib import Path

from .errors import JobError
from .settings import Settings
from .store import Store

# Engine leftovers that are never a finished file.
PARTIAL_SUFFIXES = (".part", ".ytdl", ".tmp", ".temp", ".parts")


def resolve(downloads: Path, path: str) -> Path:
    """A caller's relative path inside /data/downloads. Anything that escapes it is refused."""
    candidate = (downloads / path.lstrip("/")).resolve()
    root = downloads.resolve()
    if candidate != root and root not in candidate.parents:
        raise JobError(JobError.INVALID_REQUEST, "The path must be inside the downloads folder.")
    return candidate


def to_relative(downloads: Path, file: Path) -> str:
    return file.resolve().relative_to(downloads.resolve()).as_posix()


def folder_size(folder: Path) -> int:
    total = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                stat = os.lstat(os.path.join(root, name))
                # Blocks, not length: torrent placeholders are sparse.
                total += min(stat.st_size, stat.st_blocks * 512)
            except OSError:
                pass
    return total


def is_finished_file(file: Path) -> bool:
    return not file.name.startswith(".") and not file.name.endswith(PARTIAL_SUFFIXES)


class Disk:
    def __init__(self, downloads: Path, settings: Settings) -> None:
        self.downloads = downloads
        self.settings = settings
        self._used = 0
        self._measured = 0.0

    def used(self, fresh: bool = False) -> int:
        # Walking a big folder costs; a few seconds of staleness is fine for a quota.
        if fresh or time.monotonic() - self._measured > 5:
            self._used = folder_size(self.downloads)
            self._measured = time.monotonic()
        return self._used

    def usage(self) -> dict:
        host = shutil.disk_usage(self.downloads)
        return {
            "used_bytes": self.used(fresh=True),
            "quota_bytes": self.settings.quota_bytes,
            "host_free_bytes": host.free,
            "host_total_bytes": host.total,
            "min_free_percent": self.settings["min_free_percent"],
        }

    def check_room(self, extra: int = 0, used: int | None = None) -> None:
        """Raises quota_exceeded when `extra` more bytes would pass the quota or the host's free-space floor."""
        used = self.used() if used is None else used
        if used + extra > self.settings.quota_bytes:
            raise JobError(
                JobError.QUOTA_EXCEEDED,
                f"Not enough quota: {gib(used)} used of {gib(self.settings.quota_bytes)}, this needs {gib(extra)} more.",
            )
        host = shutil.disk_usage(self.downloads)
        floor = host.total * self.settings["min_free_percent"] / 100
        if host.free - extra < floor:
            raise JobError(
                JobError.QUOTA_EXCEEDED,
                f"The host is low on disk: {gib(host.free)} free, the floor is {gib(int(floor))}.",
            )


def gib(size: int) -> str:
    return f"{size / 1024**3:.2f} GB"


def list_files(downloads: Path, store: Store, settings: Settings, folder: str = "",
               skip_folders: set[str] = frozenset()) -> list[dict]:
    """Finished files. Job folders in skip_folders (jobs not done yet) are left out whole: while a job runs, its
    folder can hold source streams and half-converted output under finished-looking names."""
    base = resolve(downloads, folder) if folder else downloads
    if not base.is_dir():
        raise JobError(JobError.NOT_FOUND, f"No folder {folder!r}.")
    result = []
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(names):
            file = Path(root) / name
            if not is_finished_file(file):
                continue
            path = to_relative(downloads, file)
            if path.split("/", 1)[0] in skip_folders:
                continue
            created, keep = file_created(store, path, file)
            result.append({
                "path": path,
                "size": file.stat().st_size,
                "created": created,
                "deletes_at": None if keep else created + settings["cleanup_days"] * 86400,
                "pinned": keep,
            })
    return sorted(result, key=lambda item: item["created"], reverse=True)


def file_created(store: Store, path: str, file: Path) -> tuple[float, bool]:
    row = store.file_by_path(path)
    if row is not None:
        return row["created"], bool(row["keep"])
    return file.stat().st_mtime, False


def deletes_at(store: Store, settings: Settings, downloads: Path, path: str) -> float | None:
    created, keep = file_created(store, path, downloads / path)
    return None if keep else created + settings["cleanup_days"] * 86400


def delete_expired(downloads: Path, store: Store, settings: Settings, busy_folders: set[str]) -> int:
    """Hourly cleanup. Folders of running jobs are skipped whole, partial files included."""
    now = time.time()
    count = 0
    for job_folder in downloads.iterdir():
        if not job_folder.is_dir() or job_folder.name in busy_folders:
            continue
        for root, _, names in os.walk(job_folder):
            for name in names:
                file = Path(root) / name
                path = to_relative(downloads, file)
                created, keep = file_created(store, path, file)
                if not keep and now - created > settings["cleanup_days"] * 86400:
                    file.unlink(missing_ok=True)
                    store.delete_file(path)
                    count += 1
        remove_empty_folders(job_folder)
    return count


def delete_all(downloads: Path, store: Store, busy_folders: set[str], include_pinned: bool) -> dict:
    """Delete every stored file. Folders of running, queued or paused jobs are skipped whole; pinned files too,
    unless include_pinned."""
    deleted = freed = pinned = 0
    skipped = []
    for job_folder in sorted(downloads.iterdir()):
        if not job_folder.is_dir() or job_folder.name.startswith("."):
            continue
        if job_folder.name in busy_folders:
            skipped.append(job_folder.name)
            continue
        for root, _, names in os.walk(job_folder):
            for name in names:
                file = Path(root) / name
                path = to_relative(downloads, file)
                row = store.file_by_path(path)
                if row is not None and row["keep"] and not include_pinned:
                    pinned += 1
                    continue
                freed += file.stat().st_size
                file.unlink(missing_ok=True)
                store.delete_file(path)
                deleted += 1
        remove_empty_folders(job_folder)
    return {"deleted_files": deleted, "freed_bytes": freed, "kept_pinned": pinned, "skipped_active_jobs": skipped}


def remove_empty_folders(folder: Path) -> None:
    for root, dirs, _ in os.walk(folder, topdown=False):
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        folder.rmdir()
    except OSError:
        pass


def delete_folder(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)
