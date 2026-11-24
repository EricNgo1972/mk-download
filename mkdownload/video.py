"""Video sites, streams and direct media URLs through yt-dlp (as a library) and ffmpeg.

yt-dlp is synchronous, so each download runs in a worker thread. Pause and cancel set ctx.halt; the progress hook
raises Halted at the next tick, which unwinds yt-dlp and leaves its .part files for a resume.
"""

import asyncio
import copy
import logging
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError, download_range_func

from . import live, media
from .errors import Halted, JobError
from .files import Disk, is_finished_file
from .jobs import Context
from .settings import Settings
from .sites import Cookies, choose_impersonation, host_of
from .store import Job

log = logging.getLogger("mk-download.video")

DRM_HINTS = ("drm", "widevine", "playready", "fairplay")
LOGIN_HINTS = (
    "sign in", "login required", "log in", "logged-in", "--cookies", "cookies", "private video", "members-only",
    "age-restricted", "confirm your age", "registered users", "not a bot", "premium",
)
CHALLENGE_HINTS = ("cloudflare", "captcha", "challenge", "http error 403", "403: forbidden", "http error 429")
UNAVAILABLE_HINTS = (
    "video unavailable", "not available", "has been removed", "does not exist", "http error 404", "404: not found",
    "no video formats", "this video is private", "account has been terminated", "geo",
)


class Quiet:
    """yt-dlp's logger: debug noise dropped, warnings to our log, the last error kept for classify()."""

    def __init__(self) -> None:
        self.last_error = ""

    def debug(self, message: str) -> None:
        pass

    def info(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    def error(self, message: str) -> None:
        self.last_error = message
        log.info("yt-dlp: %s", message)


def classify(message: str) -> JobError:
    """yt-dlp errors are prose; this turns them into the codes the agent reacts to."""
    text = re.sub(r"^ERROR:\s*", "", message.strip())
    lowered = text.lower()
    if any(hint in lowered for hint in DRM_HINTS):
        return JobError(JobError.DRM_PROTECTED, "The stream is DRM-protected and can't be downloaded.")
    if "unsupported url" in lowered:
        return JobError(JobError.UNSUPPORTED_URL, "No extractor supports this URL.")
    if any(hint in lowered for hint in LOGIN_HINTS):
        return JobError(JobError.LOGIN_REQUIRED, f"The site wants a login; upload cookies with set_cookies. ({text[:200]})")
    if any(hint in lowered for hint in CHALLENGE_HINTS):
        return JobError(JobError.BLOCKED_BY_CHALLENGE, f"The site blocked the request. ({text[:200]})")
    if any(hint in lowered for hint in UNAVAILABLE_HINTS):
        return JobError(JobError.SOURCE_UNAVAILABLE, text[:300])
    return JobError(JobError.DOWNLOAD_FAILED, text[:300] or "yt-dlp failed without a message.")


def build_format(quality: str, audio_only: bool) -> str:
    if audio_only:
        return "ba/b"
    if quality in ("", "best"):
        return "bv*+ba/b"
    height = int(quality)
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def estimate_size(info: dict[str, Any]) -> int:
    formats = info.get("requested_formats") or [info]
    total = 0
    for item in formats:
        total += item.get("filesize") or item.get("filesize_approx") or 0
    if not total and info.get("tbr") and info.get("duration"):
        total = int(info["tbr"] * 1000 / 8 * info["duration"])
    return int(total)


class VideoEngine:
    def __init__(self, settings: Settings, disk: Disk, cookies: Cookies, scratch: Path) -> None:
        self.settings = settings
        self.disk = disk
        self.cookies = cookies
        self.scratch = scratch
        self.executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="yt-dlp")

    # options

    def build_options(
        self, url: str, folder: Path, impersonate: str | None, logger: Quiet
    ) -> tuple[dict[str, Any], str | None]:
        """yt-dlp options common to every call, and the browser to retry as once on HTTP 403/429."""
        cookie_file, cookie_browser = self.cookies.copy_for_job(url, folder)
        mode = impersonate or self.settings["impersonate"]
        if not impersonate and cookie_browser and mode not in ("auto", "off"):
            mode = cookie_browser
        target, retry = choose_impersonation(mode, url, cookie_browser)
        options: dict[str, Any] = {
            "logger": logger,
            "quiet": True,
            "no_warnings": False,
            "noprogress": True,
            "noplaylist": True,
            "retries": 10,
            "fragment_retries": 10,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "continuedl": True,
            "color": {"stdout": "never", "stderr": "never"},
            "cachedir": str(self.scratch / "yt-dlp-cache"),
        }
        if cookie_file is not None:
            options["cookiefile"] = str(cookie_file)
        if target:
            options["impersonate"] = ImpersonateTarget.from_str(target)
        if self.settings["proxy_url"]:
            options["proxy"] = self.settings["proxy_url"]
        if self.settings["speed_limit_kbps"]:
            options["ratelimit"] = self.settings["speed_limit_kbps"] * 1024
        return options, retry

    def run_with_retry(self, url: str, folder: Path, impersonate: str | None, call) -> Any:
        """Runs call(options, logger); on a 403/429 without impersonation, once more as the retry browser."""
        logger = Quiet()
        options, retry = self.build_options(url, folder, impersonate, logger)
        try:
            return call(options, logger)
        except DownloadError as error:
            message = logger.last_error or str(error)
            blocked = "403" in message or "429" in message
            if not (blocked and retry and "impersonate" not in options):
                raise classify(message) from None
        log.info("retrying %s as %s after a block", host_of(url), retry)
        logger = Quiet()
        options, _ = self.build_options(url, folder, retry, logger)
        try:
            return call(options, logger)
        except DownloadError as error:
            raise classify(logger.last_error or str(error)) from None

    async def in_thread(self, ctx: Context | None, function, *args) -> Any:
        """Runs a blocking call; on cancellation it asks the thread to halt and waits for it, so files are free."""
        future = asyncio.get_running_loop().run_in_executor(self.executor, function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if ctx is not None:
                ctx.halt.set()
            with suppress(BaseException):
                await future
            raise

    # probe and transcript

    async def probe(self, url: str, impersonate: str | None = None) -> dict[str, Any]:
        return await self.in_thread(None, self.probe_sync, url, impersonate)

    def probe_sync(self, url: str, impersonate: str | None) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(dir=self.scratch) as temp:

            def call(options, logger):
                options.update({"skip_download": True, "ignore_no_formats_error": True, "extract_flat": "in_playlist"})
                with yt_dlp.YoutubeDL(options) as ydl:
                    return ydl.sanitize_info(ydl.extract_info(url, download=False))

            info = self.run_with_retry(url, Path(temp), impersonate, call)
        return describe(info)

    async def transcript(self, url: str, lang: str) -> dict[str, Any]:
        return await self.in_thread(None, self.transcript_sync, url, lang)

    def transcript_sync(self, url: str, lang: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(dir=self.scratch) as temp:
            folder = Path(temp)

            def call(options, logger):
                options.update({
                    "skip_download": True,
                    "writesubtitles": True,
                    "writeautomaticsub": True,
                    "subtitleslangs": [lang, f"{lang}.*", f"{lang}-.*"],
                    "subtitlesformat": "vtt/srt/best",
                    "outtmpl": {"default": str(folder / "transcript.%(ext)s")},
                })
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                    requested = info.get("requested_subtitles") or {}
                    if not requested:
                        return info, None
                    # Uploaded subtitles win over auto-generated ones of the same language.
                    uploaded = info.get("subtitles") or {}
                    chosen = next((code for code in requested if code in uploaded), next(iter(requested)))
                    info["requested_subtitles"] = {chosen: requested[chosen]}
                    ydl.process_info(info)
                    return info, chosen

            info, chosen = self.run_with_retry(url, folder, None, call)
            if chosen is None:
                available = sorted(set(info.get("subtitles") or {}) | set(info.get("automatic_captions") or {}))
                raise JobError(
                    JobError.SOURCE_UNAVAILABLE,
                    f"No {lang} subtitles. Available: {', '.join(available[:40]) or 'none'}.",
                )
            subtitle = next((file for file in folder.iterdir() if file.suffix in (".vtt", ".srt")), None)
            if subtitle is None:
                raise JobError(JobError.DOWNLOAD_FAILED, "The subtitle file was not written.")
            is_auto = chosen not in (info.get("subtitles") or {})
            return {
                "title": info.get("title"),
                "language": chosen,
                "auto_generated": is_auto,
                "text": to_plain_text(subtitle.read_text(errors="replace")),
            }

    # jobs

    async def run(self, job: Job, ctx: Context) -> list[Path]:
        if job.is_live:
            return await live.record(self, job, ctx)
        return await self.download(job, ctx)

    async def discard(self, job: Job) -> None:
        pass

    async def download(self, job: Job, ctx: Context) -> list[Path]:
        params = job.params
        ranges = media.parse_clips(params.get("clip"))
        info = await self.in_thread(ctx, self.download_sync, job, ctx, ranges)
        outputs = sorted(file for file in ctx.folder.iterdir() if file.is_file() and is_finished_file(file))
        if ranges and (params.get("subtitles") or params.get("subtitles_only")):
            await self.cut_clip_subtitles(ctx.folder, outputs, ranges, bool(params.get("embed_subs")))
        if not outputs:
            raise JobError(JobError.DOWNLOAD_FAILED, f"Nothing was downloaded for {info.get('title') or job.url}.")
        return outputs

    def download_sync(self, job: Job, ctx: Context, ranges: list[tuple[float, float | None]]) -> dict[str, Any]:
        params = job.params
        quality = str(params.get("quality") or self.settings["default_quality"])
        audio_only = bool(params.get("audio_only"))
        subtitles_only = bool(params.get("subtitles_only"))
        sub_format = params.get("sub_format") or self.settings["sub_format"]
        embed = params.get("embed_subs")
        embed = self.settings["embed_subs"] if embed is None else bool(embed)
        precise = params.get("precise")
        precise = self.settings["precise_clips"] if precise is None else bool(precise)
        languages = params.get("subtitles")
        if languages == "all" or languages == ["all"]:
            languages = ["all", "-live_chat"]
        elif isinstance(languages, str):
            languages = [languages]
        wants_subs = bool(languages) or subtitles_only
        if subtitles_only and not languages:
            languages = ["en.*", "en"]
        progress = Tracker(ctx, self.disk)

        def hook(status: dict[str, Any]) -> None:
            if ctx.halt.is_set():
                raise Halted()
            progress.update(status)

        def postprocessor_hook(status: dict[str, Any]) -> None:
            if ctx.halt.is_set():
                raise Halted()
            if status.get("status") == "started" and status.get("postprocessor") not in ("MoveFiles",):
                ctx.set_status("finalizing")

        def configure(options: dict[str, Any]) -> None:
            name = media.safe_name(params["name"]) if params.get("name") else "%(title).150B [%(id)s]"
            options.update({
                "format": build_format(quality, audio_only),
                "outtmpl": {"default": str(ctx.folder / f"{name}.%(ext)s")},
                "progress_hooks": [hook],
                "postprocessor_hooks": [postprocessor_hook],
                "skip_download": subtitles_only,
                "writesubtitles": wants_subs,
                "writeautomaticsub": wants_subs and bool(params.get("auto_subs")),
                "subtitleslangs": languages or [],
                "subtitlesformat": f"{sub_format}/vtt/srt/best",
                "postprocessors": [],
            })
            if not audio_only:
                options["format_sort"] = ["res", "ext:mp4:m4a"]
                options["merge_output_format"] = "mp4"
            else:
                options["postprocessors"].append({"key": "FFmpegExtractAudio", "preferredcodec": "m4a"})
            if wants_subs:
                options["postprocessors"].append(
                    {"key": "FFmpegSubtitlesConvertor", "format": sub_format, "when": "before_dl"}
                )
            if wants_subs and embed and not ranges and not subtitles_only and not audio_only:
                options["postprocessors"].append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": True})

        def call(options, logger):
            configure(options)
            with yt_dlp.YoutubeDL(options) as ydl:
                try:
                    info = ydl.extract_info(job.url, download=False)
                except Halted:
                    raise
                check_not_live(info)
                if info.get("_type") == "playlist":
                    raise JobError(JobError.INVALID_REQUEST, "This URL is a playlist or channel; download its videos one by one.")
                estimate = estimate_size(info)
                if ranges and info.get("duration"):
                    covered = sum((end or info["duration"]) - start for start, end in ranges)
                    estimate = int(estimate * min(1.0, covered / info["duration"]))
                if not subtitles_only:
                    self.disk.check_room(estimate)
                progress.expected = estimate
                ctx.report(title=info.get("title"), total_bytes=estimate or None)
                if not ranges:
                    ydl.process_ie_result(info, download=True)
                    return info
            # One pass per clip range, each into its own file.
            for index, (start, end) in enumerate(ranges):
                suffix = f" [clip {media.to_clock(start)}-{media.to_clock(end) if end is not None else 'end'}]"
                pass_options = dict(options)
                pass_options["outtmpl"] = {"default": options["outtmpl"]["default"].replace(".%(ext)s", suffix + ".%(ext)s")}
                pass_options["download_ranges"] = download_range_func(None, [(start, end if end is not None else float("inf"))])
                pass_options["force_keyframes_at_cuts"] = precise
                progress.passes = (index, len(ranges))
                with yt_dlp.YoutubeDL(pass_options) as ydl:
                    ydl.process_ie_result(copy.deepcopy(info), download=True)
            return info

        try:
            return self.run_with_retry(job.url, ctx.folder, params.get("impersonate"), call)
        except Halted:
            raise asyncio.CancelledError() from None

    async def cut_clip_subtitles(
        self, folder: Path, outputs: list[Path], ranges: list[tuple[float, float | None]], embed: bool
    ) -> None:
        """yt-dlp writes the whole video's subtitles next to each clip; cut each to its clip's range."""
        for start, end in ranges:
            marker = f" [clip {media.to_clock(start)}-{media.to_clock(end) if end is not None else 'end'}]"
            subtitles = [file for file in outputs if marker in file.name and file.suffix in (".srt", ".vtt")]
            for subtitle in subtitles:
                await media.cut_subtitle(subtitle, start, end)
            videos = [file for file in outputs if marker in file.name and file.suffix in (".mp4", ".mkv", ".webm")]
            if embed:
                for video in videos:
                    await media.embed_subtitles(video, subtitles)

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


class Tracker:
    """Folds yt-dlp's per-file progress (video, then audio, then each clip) into one percent for the job."""

    def __init__(self, ctx: Context, disk: Disk) -> None:
        self.ctx = ctx
        self.disk = disk
        self.expected = 0
        self.passes = (0, 1)
        self.files: dict[str, tuple[int, int]] = {}
        self.reported = 0.0
        self.checked = 0.0
        self.base_used = disk.used(fresh=True)

    def update(self, status: dict[str, Any]) -> None:
        if status.get("status") not in ("downloading", "finished"):
            return
        name = status.get("filename") or status.get("tmpfilename") or "?"
        done = status.get("downloaded_bytes") or 0
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or done
        self.files[name] = (done, total)
        now = time.monotonic()
        if now - self.checked > 5:
            self.checked = now
            self.disk.check_room(0, used=self.base_used + sum(d for d, _ in self.files.values()))
        if now - self.reported < 1 and status.get("status") != "finished":
            return
        self.reported = now
        downloaded = sum(d for d, _ in self.files.values())
        known = sum(t for _, t in self.files.values())
        expected = max(self.expected, known)
        index, count = self.passes
        percent = (downloaded / expected * 100) if expected else 0.0
        if count > 1:
            percent = (index * 100 + min(percent, 100)) / count
        self.ctx.report(
            percent=round(min(percent, 99.9), 1),
            speed=status.get("speed"),
            eta=status.get("eta"),
            downloaded_bytes=downloaded,
            total_bytes=expected or None,
        )


def check_not_live(info: dict[str, Any]) -> None:
    status = info.get("live_status")
    if status == "is_live":
        raise JobError(JobError.IS_LIVE, "This is a live stream; call download again with live: true to record it.")
    if status == "is_upcoming":
        raise JobError(
            JobError.NOT_LIVE, "This live stream hasn't started; use live: true with wait_for_start to record it."
        )


def describe(info: dict[str, Any]) -> dict[str, Any]:
    """probe's answer: what the agent needs to choose quality, subtitles, clips and live options."""
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        return {
            "type": "playlist",
            "title": info.get("title"),
            "count": info.get("playlist_count") or len(entries),
            "entries": [{"title": e.get("title"), "url": e.get("url") or e.get("webpage_url")} for e in entries[:50]],
        }
    heights: dict[int, int] = {}
    audio = 0
    for item in info.get("formats") or []:
        size = item.get("filesize") or item.get("filesize_approx") or 0
        if item.get("vcodec") not in (None, "none") and item.get("height"):
            heights[item["height"]] = max(heights.get(item["height"], 0), size)
        elif item.get("acodec") not in (None, "none"):
            audio = max(audio, size)
    qualities = [
        {"height": height, "approx_size": (size + audio) or None}
        for height, size in sorted(heights.items(), reverse=True)
    ]
    subtitles = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    return {
        "type": "video",
        "id": info.get("id"),
        "title": info.get("title"),
        "site": info.get("extractor_key"),
        "url": info.get("webpage_url") or info.get("original_url"),
        "uploader": info.get("uploader") or info.get("channel"),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "live_status": info.get("live_status") or "not_live",
        "starts_at": info.get("release_timestamp") if info.get("live_status") == "is_upcoming" else None,
        "qualities": qualities,
        "approx_size_best": estimate_size(info) or None,
        "subtitles": sorted(lang for lang in subtitles if lang != "live_chat"),
        "auto_subtitles": sorted(automatic)[:200],
        "has_drm": bool(info.get("_has_drm")),
        "thumbnail": info.get("thumbnail"),
    }


def to_plain_text(subtitle: str) -> str:
    """VTT/SRT to readable text: no cue numbers, timings or tags, and no rolling duplicates of auto captions."""
    lines: list[str] = []
    for raw in subtitle.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.isdigit() or "-->" in line:
            continue
        if line.startswith(("Kind:", "Language:", "NOTE", "STYLE", "REGION")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"&nbsp;", " ", line).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)
