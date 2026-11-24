"""ffmpeg helpers shared by the video engine, the live recorder and clip_file."""

import asyncio
import re
from pathlib import Path

from .errors import JobError

TIME_PATTERN = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(text: str | int | float | None) -> float | None:
    """ss, mm:ss or hh:mm:ss (fractions allowed) to seconds; None or "" means open."""
    if text is None or text == "":
        return None
    if isinstance(text, int | float):
        return float(text)
    match = TIME_PATTERN.match(str(text).strip())
    if not match:
        raise JobError(JobError.INVALID_REQUEST, f"{text!r} is not a time; use ss, mm:ss or hh:mm:ss.")
    first, second, seconds = match.groups()
    if second is not None:
        hours, minutes = int(first), int(second)
    else:
        hours, minutes = 0, int(first or 0)
    return hours * 3600 + minutes * 60 + float(seconds)


def to_clock(seconds: float) -> str:
    """For file names: 1:05 -> 0105, 1:02:03 -> 010203."""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours:02d}{minutes:02d}{secs:02d}" if hours else f"{minutes:02d}{secs:02d}"


def parse_clips(clips: list[dict] | None) -> list[tuple[float, float | None]]:
    ranges = []
    for clip in clips or []:
        start = parse_time(clip.get("start")) or 0.0
        end = parse_time(clip.get("end"))
        if end is not None and end <= start:
            raise JobError(JobError.INVALID_REQUEST, f"Clip end {clip.get('end')} must be after start {clip.get('start')}.")
        ranges.append((start, end))
    return ranges


async def run_ffmpeg(*args: str, timeout: float | None = None) -> None:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(timeout):
            _, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()[-3:]
        raise JobError(JobError.DOWNLOAD_FAILED, "ffmpeg failed: " + " / ".join(detail))


def cut_args(source: Path, start: float, end: float | None, precise: bool, target: Path) -> list[str]:
    """Copy mode seeks to the nearest keyframe before start; precise mode re-encodes for an exact cut."""
    if precise:
        args = ["-i", str(source), "-ss", f"{start:.3f}"]
        if end is not None:
            args += ["-to", f"{end:.3f}"]
        return args + [
            "-map", "0:v?", "-map", "0:a?", "-map", "0:s?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-c:s", "copy",
            "-movflags", "+faststart", str(target),
        ]
    args = ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    return args + [
        "-i", str(source), "-map", "0", "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", str(target),
    ]


async def cut_subtitle(source: Path, start: float, end: float | None) -> None:
    """Cuts a .srt/.vtt in place to a clip's range, so its times start at the clip's zero."""
    target = source.with_name(source.stem + ".cut" + source.suffix)
    args = ["-ss", f"{start:.3f}"]
    if end is not None:
        args += ["-to", f"{end:.3f}"]
    await run_ffmpeg(*args, "-i", str(source), str(target), timeout=120)
    target.replace(source)


async def embed_subtitles(video: Path, subtitles: list[Path]) -> None:
    """Adds subtitle files to an MP4 as mov_text tracks, tagged with the language from the file name."""
    if not subtitles:
        return
    target = video.with_name(video.stem + ".subs" + video.suffix)
    args = ["-i", str(video)]
    for subtitle in subtitles:
        args += ["-i", str(subtitle)]
    args += ["-map", "0"]
    for index in range(len(subtitles)):
        args += ["-map", str(index + 1)]
    args += ["-c", "copy", "-c:s", "mov_text" if video.suffix == ".mp4" else "copy"]
    for index, subtitle in enumerate(subtitles):
        language = subtitle.suffixes[-2].lstrip(".") if len(subtitle.suffixes) >= 2 else "und"
        args += [f"-metadata:s:s:{index}", f"language={language}"]
    await run_ffmpeg(*args, str(target), timeout=1800)
    target.replace(video)


def safe_name(name: str, limit: int = 150) -> str:
    """A file name from a title or a caller's name: no path separators or control characters, bounded length."""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', " ", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    encoded = cleaned.encode()[:limit]
    return encoded.decode(errors="ignore").strip() or "download"
