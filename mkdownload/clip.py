"""clip_file: cuts a stored video into a new job's folder. Copy mode is fast and lands on a keyframe; precise re-encodes."""

from pathlib import Path

from . import media
from .errors import JobError
from .files import resolve
from .jobs import Context
from .settings import Settings
from .store import Job


class ClipEngine:
    def __init__(self, downloads: Path, settings: Settings) -> None:
        self.downloads = downloads
        self.settings = settings

    async def run(self, job: Job, ctx: Context) -> list[Path]:
        params = job.params
        source = resolve(self.downloads, params["path"])
        if not source.is_file():
            raise JobError(JobError.NOT_FOUND, f"No file {params['path']}.")
        start = media.parse_time(params.get("start")) or 0.0
        end = media.parse_time(params.get("end"))
        precise = params.get("precise")
        precise = self.settings["precise_clips"] if precise is None else bool(precise)
        stem = media.safe_name(params["name"]) if params.get("name") else source.stem
        clock = f"{media.to_clock(start)}-{media.to_clock(end) if end is not None else 'end'}"
        suffix = ".mp4" if precise else source.suffix
        target = ctx.folder / f"{stem} [clip {clock}]{suffix}"
        ctx.report(percent=0.0, title=source.name)
        await media.run_ffmpeg(*media.cut_args(source, start, end, precise, target), timeout=6 * 3600)
        return [target]

    async def discard(self, job: Job) -> None:
        pass
