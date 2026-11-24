"""Live recording.

yt-dlp finds the stream; ffmpeg records it to 5-minute .ts segments, so a crash or restart loses nothing already
written. A dropped connection restarts ffmpeg at the next segment number. stop, the stream's end, max_duration and
the disk quota all end the recording and keep it: the segments are joined into one MP4.

YouTube's from_start (record from the broadcast's beginning) is yt-dlp's own downloader instead.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yt_dlp
from yt_dlp.utils import DownloadError

from . import media
from .errors import Halted, JobError
from .files import folder_size, is_finished_file
from .jobs import Context
from .store import Job

if TYPE_CHECKING:
    from .video import VideoEngine

log = logging.getLogger("mk-download.live")

SEGMENT_SECONDS = 300
POLL_SECONDS = 2
WAIT_POLL_SECONDS = 60
MAX_RECONNECTS = 20


def build_live_format(quality: str) -> str:
    # HLS first: ffmpeg follows a live playlist natively.
    if quality in ("", "best"):
        return "b[protocol^=m3u8]/b/bv*+ba"
    height = int(quality)
    return f"b[height<={height}][protocol^=m3u8]/b[height<={height}]/b[protocol^=m3u8]/b"


async def sleep_unless(ctx: Context, seconds: float) -> None:
    """Sleeps, waking early when stop is requested. Pause and cancel arrive as task cancellation."""
    end = time.monotonic() + seconds
    while time.monotonic() < end and not ctx.stop.is_set():
        await asyncio.sleep(min(1.0, end - time.monotonic()))


async def record(engine: "VideoEngine", job: Job, ctx: Context) -> list[Path]:
    params = job.params
    settings = engine.settings
    segments = ctx.folder / ".segments"
    segments.mkdir(exist_ok=True)
    max_seconds = float(params.get("max_duration") or settings["live_max_hours"] * 3600)
    wait_limit = min(float(params.get("wait_for_start") or 0), settings["live_wait_max_hours"] * 3600)
    quality = str(params.get("quality") or settings["default_quality"])
    name = media.safe_name(params["name"]) if params.get("name") else None

    if ctx.stop.is_set():  # stop on a paused or queued recording: only finish what is on disk
        return await finalize(job, ctx, segments, name or job.progress.get("title") or job.id)

    deadline = time.time() + wait_limit
    while True:
        info = await engine.in_thread(ctx, extract_stream, engine, job, ctx.folder, quality)
        status = info.get("live_status")
        name = name or media.safe_name(info.get("title") or job.id)
        if status == "is_live":
            break
        if status == "is_upcoming" and time.time() < deadline and not ctx.stop.is_set():
            ctx.set_status("waiting")
            ctx.report(title=info.get("title"), starts_at=info.get("release_timestamp"))
            await sleep_unless(ctx, WAIT_POLL_SECONDS)
            continue
        if any(segments.glob("seg_*.ts")):  # resumed after the stream ended: keep what was recorded
            return await finalize(job, ctx, segments, name)
        if ctx.stop.is_set():
            raise JobError(JobError.NOT_LIVE, "Stopped before the stream started.")
        if status == "is_upcoming":
            raise JobError(JobError.NOT_LIVE, "The stream hasn't started" + (" within wait_for_start." if wait_limit else "; pass wait_for_start to wait for it."))
        raise JobError(JobError.NOT_LIVE, f"The stream is not live (status: {status or 'not_live'}).")

    ctx.set_status("recording")
    ctx.report(title=info.get("title"), started_at=time.time())
    if params.get("from_start"):
        return await record_from_start(engine, job, ctx, quality, max_seconds, name)

    recorded = float(job.progress.get("recorded_seconds") or 0)
    stopped_by = ""
    failures = 0
    is_reconnect = False
    while True:
        remaining = max_seconds - recorded
        if remaining <= 1:
            stopped_by = "max_duration"
            break
        started = time.monotonic()
        outcome, content = await run_segmenter(engine, ctx, info, segments, remaining, recorded, is_reconnect)
        # Content time, not wall time: a reconnect reads the playlist's buffered segments faster than real time.
        recorded += content
        ctx.report(recorded_seconds=round(recorded))
        if outcome:
            stopped_by = outcome
            break
        if recorded >= max_seconds - 1:
            stopped_by = "max_duration"
            break
        # ffmpeg ended on its own: the stream ended or the connection dropped. Ask the site which.
        failures = failures + 1 if time.monotonic() - started < 30 else 0
        if failures > MAX_RECONNECTS:
            stopped_by = "connection"
            break
        await sleep_unless(ctx, min(3 * failures + 2, 30))
        if ctx.stop.is_set():
            stopped_by = "stop"
            break
        try:
            info = await engine.in_thread(ctx, extract_stream, engine, job, ctx.folder, quality)
        except JobError as error:
            log.info("job %s: re-probe failed (%s); retrying", job.id, error.message)
            continue
        if info.get("live_status") != "is_live":
            stopped_by = "stream_ended"
            break
        is_reconnect = True
        log.info("job %s: reconnecting to the live stream", job.id)
    ctx.report(stopped_by=stopped_by)
    return await finalize(job, ctx, segments, name)


def extract_stream(engine: "VideoEngine", job: Job, folder: Path, quality: str) -> dict[str, Any]:
    def call(options, logger):
        options.update({"format": build_live_format(quality), "ignore_no_formats_error": True, "skip_download": True})
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(job.url, download=False)

    return engine.run_with_retry(job.url, folder, job.params.get("impersonate"), call)


async def run_segmenter(
    engine: "VideoEngine",
    ctx: Context,
    info: dict[str, Any],
    segments: Path,
    seconds: float,
    recorded: float,
    is_reconnect: bool,
) -> tuple[str, float]:
    """Records until ffmpeg ends by itself (outcome ""), or until stop/quota (outcome says why).
    Returns the outcome and the seconds of content written, from ffmpeg's own progress report."""
    streams = info.get("requested_formats") or [info]
    if not streams[0].get("url"):
        raise JobError(JobError.SOURCE_UNAVAILABLE, "The live stream has no playable format.")
    start_number = len(list(segments.glob("seg_*.ts")))
    args = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats", "-y"]
    for stream in streams:
        headers = "".join(f"{key}: {value}\r\n" for key, value in (stream.get("http_headers") or {}).items())
        if headers:
            args += ["-headers", headers]
        if is_reconnect and str(stream.get("protocol", "")).startswith("m3u8"):
            # Start at the newest segment, so a reconnect doesn't record the buffered minute again.
            args += ["-live_start_index", "-1"]
        args += ["-i", stream["url"]]
    if len(streams) > 1:
        args += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        args += ["-map", "0:v?", "-map", "0:a?"]
    args += [
        "-c", "copy", "-t", f"{seconds:.0f}", "-progress", "pipe:1",
        "-f", "segment", "-segment_time", str(SEGMENT_SECONDS), "-segment_start_number", str(start_number),
        "-reset_timestamps", "1", str(segments / "seg_%05d.ts"),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stderr_tail: list[str] = []
    content = [0.0]

    async def read_stderr() -> None:
        while line := await process.stderr.readline():
            stderr_tail.append(line.decode(errors="replace").rstrip())
            del stderr_tail[:-5]

    async def read_progress() -> None:
        while line := await process.stdout.readline():
            key, _, value = line.decode(errors="replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit():
                content[0] = int(value) / 1_000_000

    readers = [asyncio.create_task(read_stderr()), asyncio.create_task(read_progress())]
    outcome = ""
    try:
        while process.returncode is None:
            try:
                async with asyncio.timeout(POLL_SECONDS):
                    await process.wait()
                break
            except TimeoutError:
                pass
            ctx.report(recorded_seconds=round(recorded + content[0]), downloaded_bytes=folder_size(segments))
            if ctx.stop.is_set():
                outcome = "stop"
            else:
                try:
                    engine.disk.check_room(0, used=engine.disk.used())
                except JobError:
                    outcome = "quota"
            if outcome:
                await quit_gracefully(process)
                break
        if process.returncode and not outcome:
            log.info("ffmpeg exited %s: %s", process.returncode, " / ".join(stderr_tail))
    finally:
        # Pause, cancel or shutdown: ffmpeg must close its segment cleanly before the task ends.
        if process.returncode is None:
            await quit_gracefully(process)
        await asyncio.gather(*readers)
    return outcome, content[0]


async def quit_gracefully(process: asyncio.subprocess.Process) -> None:
    """ffmpeg's 'q' closes the current segment properly; kill only if it ignores us."""
    try:
        process.stdin.write(b"q")
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    try:
        async with asyncio.timeout(15):
            await asyncio.shield(process.wait())
    except TimeoutError:
        process.kill()
        await process.wait()


async def finalize(job: Job, ctx: Context, segments: Path, name: str) -> list[Path]:
    parts = sorted(segments.glob("seg_*.ts"))
    parts = [part for part in parts if part.stat().st_size > 0]
    if not parts:
        raise JobError(JobError.DOWNLOAD_FAILED, "Nothing was recorded.")
    ctx.set_status("finalizing")
    stamp = time.strftime("%Y-%m-%d %H%M", time.localtime(job.started or time.time()))
    target = ctx.folder / f"{name} [live {stamp}].mp4"
    listing = segments / "list.txt"
    listing.write_text("".join(f"file '{part.name}'\n" for part in parts))
    try:
        await media.run_ffmpeg(
            "-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0", "-c", "copy",
            "-movflags", "+faststart", str(target), timeout=3 * 3600,
        )
    except JobError as error:
        # Never lose a recording to a failed join: hand out the segments instead.
        log.warning("job %s: joining segments failed (%s); keeping them as .ts files", job.id, error.message)
        target.unlink(missing_ok=True)
        kept = ctx.folder / "segments"
        segments.rename(kept)
        (kept / "list.txt").unlink(missing_ok=True)
        return sorted(kept.glob("seg_*.ts"))
    for part in segments.iterdir():
        part.unlink(missing_ok=True)
    segments.rmdir()
    return [target]


async def record_from_start(
    engine: "VideoEngine", job: Job, ctx: Context, quality: str, max_seconds: float, name: str
) -> list[Path]:
    """yt-dlp's live-from-start downloader. stop and max_duration (wall time) interrupt it; the partial
    video and audio it wrote are then merged by ffmpeg."""
    started = time.monotonic()
    stopped = {"why": ""}

    def hook(status: dict[str, Any]) -> None:
        if ctx.halt.is_set():
            raise Halted()
        elapsed = time.monotonic() - started
        if ctx.stop.is_set() or elapsed >= max_seconds:
            stopped["why"] = "stop" if ctx.stop.is_set() else "max_duration"
            raise Halted()
        ctx.report(recorded_seconds=round(elapsed), downloaded_bytes=status.get("downloaded_bytes"))

    def download() -> None:
        def call(options, logger):
            options.update({
                "format": "bv*+ba/b" if quality in ("", "best") else f"bv*[height<={quality}]+ba/b",
                "live_from_start": True,
                "outtmpl": {"default": str(ctx.folder / f"{name} [live from start].%(ext)s")},
                "merge_output_format": "mp4",
                "progress_hooks": [hook],
            })
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([job.url])

        engine.run_with_retry(job.url, ctx.folder, job.params.get("impersonate"), call)

    try:
        await engine.in_thread(ctx, download)
    except Halted:
        if not stopped["why"]:
            raise asyncio.CancelledError() from None
        ctx.report(stopped_by=stopped["why"])
        return await merge_partials(ctx, name)
    except (JobError, DownloadError):
        if any(ctx.folder.glob("*.part")):
            return await merge_partials(ctx, name)
        raise
    return sorted(file for file in ctx.folder.iterdir() if file.is_file() and is_finished_file(file))


async def merge_partials(ctx: Context, name: str) -> list[Path]:
    ctx.set_status("finalizing")
    parts = sorted(ctx.folder.glob("*.part"), key=lambda file: file.stat().st_size, reverse=True)[:2]
    if not parts:
        raise JobError(JobError.DOWNLOAD_FAILED, "Nothing was recorded.")
    target = ctx.folder / f"{name} [live from start].mp4"
    args: list[str] = []
    for part in parts:
        args += ["-i", str(part)]
    for index in range(len(parts)):
        args += ["-map", str(index)]
    await media.run_ffmpeg(*args, "-c", "copy", "-movflags", "+faststart", str(target), timeout=3 * 3600)
    for leftover in ctx.folder.iterdir():
        if leftover.is_file() and leftover != target and not is_finished_file(leftover):
            leftover.unlink(missing_ok=True)
    return [target]
