"""The 18 MCP tools the agent operates the service with. Downloads are asynchronous: download returns a job id at
once; poll get_job, or pass callback_url to be told by webhook.

Errors come back as tool errors whose text is JSON: {"error": "<code>", "message": "<one sentence>"}.
"""

import base64
import json
import shutil
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import files, torrent
from .errors import JobError
from .service import Service, versions
from .settings import SETTINGS
from .sites import check_site

INSTRUCTIONS = """\
mk-download downloads videos (YouTube, TikTok, Instagram/Facebook Reels, Bilibili, ~1800 other sites, raw .m3u8/.mpd),
torrents (magnet, .torrent) and eD2k links, and hands out expiring links to the files.

Typical flow: probe(url) to see title, qualities, subtitles and live state -> download(url, ...) returns job_id ->
get_job(job_id) until status is completed or failed -> get_link(path) for a shareable URL.
Live streams: download(url, live=true); stop(job_id) ends a recording and keeps it.
Errors are JSON with a code: quota_exceeded, login_required (upload cookies), blocked_by_challenge, drm_protected,
not_live, is_live, unsupported_url, source_unavailable, queue_full, site_blocked, engine_disabled, invalid_request,
not_found, download_failed, internal_error.
"""


class ClipRange(BaseModel):
    start: str = Field(description="Start time: ss, mm:ss or hh:mm:ss.")
    end: str | None = Field(default=None, description="End time; omit to run to the end of the video.")


def fail(error: JobError) -> ToolError:
    return ToolError(json.dumps({"error": error.code, "message": error.message}))


def route(url: str) -> str:
    lowered = url.strip().lower()
    if lowered.startswith("ed2k://"):
        return "ed2k"
    if torrent.is_torrent_url(lowered):
        return "torrent"
    if not lowered.startswith(("http://", "https://")):
        raise JobError(JobError.UNSUPPORTED_URL, "Use an http(s) URL, a magnet link or an ed2k:// link.")
    return "video"


def check_callback(service: Service, callback_url: str | None) -> None:
    if callback_url is None:
        return
    if not callback_url.lower().startswith(("https://", "http://")):
        raise JobError(JobError.INVALID_REQUEST, "callback_url must be an http(s) URL.")
    if not service.env.webhook_secret:
        raise JobError(JobError.INVALID_REQUEST, "Webhooks need WEBHOOK_SECRET set on the tenant; poll get_job instead.")


def build_server(service: Service) -> MCPServer:
    server = MCPServer(name="mk-download", instructions=INSTRUCTIONS, version="1.0.0")
    jobs = service.jobs
    settings = service.settings

    @server.tool()
    async def probe(url: Annotated[str, Field(description="Video page, stream, magnet, .torrent or ed2k:// URL.")]) -> dict[str, Any]:
        """Look at a URL without downloading. Video: title, length, qualities with sizes, subtitle languages
        (uploaded and auto-generated), live state. Torrent/magnet: name, size, file list with indexes. eD2k: name, size."""
        try:
            kind = route(url)
            check_site(url, settings)
            if kind == "ed2k":
                return service.ed2k.probe(url)
            if kind == "torrent":
                return await service.torrent.probe(url)
            return await service.video.probe(url)
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def download(
        url: Annotated[str, Field(description="Video page, stream (.m3u8/.mpd), magnet, .torrent URL or ed2k:// link.")],
        quality: Annotated[Literal["best", "2160", "1440", "1080", "720", "480", "360"] | None, Field(description="Maximum height; default from settings (best).")] = None,
        audio_only: Annotated[bool, Field(description="Audio only, as .m4a.")] = False,
        subtitles: Annotated[list[str] | Literal["all"] | None, Field(description='Subtitle languages, e.g. ["en","vi"], or "all".')] = None,
        auto_subs: Annotated[bool, Field(description="Allow auto-generated subtitles.")] = False,
        sub_format: Annotated[Literal["srt", "vtt"] | None, Field(description="Subtitle format; default from settings.")] = None,
        embed_subs: Annotated[bool | None, Field(description="Also put subtitles inside the MP4 as tracks.")] = None,
        subtitles_only: Annotated[bool, Field(description="Only the subtitle files, no video.")] = False,
        clip: Annotated[list[ClipRange] | None, Field(description="Download only these time ranges; one file per range.")] = None,
        precise: Annotated[bool | None, Field(description="Cut clips at the exact second (re-encodes, slower).")] = None,
        files: Annotated[list[int] | None, Field(description="Torrent only: indexes of the files to download (from probe).")] = None,
        live: Annotated[bool, Field(description="Record a live stream.")] = False,
        max_duration: Annotated[float | None, Field(description="Live: stop after this many seconds; default from settings.")] = None,
        from_start: Annotated[bool, Field(description="Live: record from the broadcast start where the site allows (YouTube).")] = False,
        wait_for_start: Annotated[float | None, Field(description="Live: wait up to this many seconds for a scheduled stream to start.")] = None,
        impersonate: Annotated[Literal["chrome", "safari", "firefox", "edge", "off"] | None, Field(description="Override browser impersonation; default auto.")] = None,
        name: Annotated[str | None, Field(description="Output file name without extension.")] = None,
        keep: Annotated[bool, Field(description="Exempt the files from automatic cleanup.")] = False,
        callback_url: Annotated[str | None, Field(description="POST the result here when the job completes, fails or is cancelled.")] = None,
        callback_ref: Annotated[str | None, Field(description="Your own id, echoed in the webhook.")] = None,
        notify_progress: Annotated[bool, Field(description="Also send started and 25/50/75% webhooks.")] = False,
    ) -> dict[str, Any]:
        """Start a download and return its job_id at once. Video sites go through yt-dlp, torrents through
        libtorrent, eD2k through Kelpie. Poll get_job, or pass callback_url for a signed webhook on completion."""
        try:
            kind = route(url)
            check_site(url, settings)
            check_callback(service, callback_url)
            if kind != "video" and (live or clip or subtitles or subtitles_only or audio_only):
                raise JobError(JobError.INVALID_REQUEST, "live, clip, subtitles and audio_only apply to video URLs only.")
            if kind == "torrent":
                service.torrent.ensure_session()
            if kind == "ed2k":
                service.ed2k.ensure()
            if live and clip:
                raise JobError(JobError.INVALID_REQUEST, "A live recording can't be clipped while recording; use clip_file after.")
            if live and settings["max_live"] == 0:
                raise JobError(JobError.ENGINE_DISABLED, "Live recording is turned off (0 live slots).")
            params = {
                "quality": quality,
                "audio_only": audio_only,
                "subtitles": subtitles,
                "auto_subs": auto_subs,
                "sub_format": sub_format,
                "embed_subs": embed_subs,
                "subtitles_only": subtitles_only,
                "clip": [item.model_dump() for item in clip] if clip else None,
                "precise": precise,
                "files": files,
                "max_duration": max_duration,
                "from_start": from_start,
                "wait_for_start": wait_for_start,
                "impersonate": impersonate,
                "name": name,
            }
            if clip:
                from .media import parse_clips
                parse_clips(params["clip"])
            job = jobs.add(
                kind, url.strip(), {k: v for k, v in params.items() if v not in (None, False)},
                is_live=live, callback_url=callback_url, callback_ref=callback_ref,
                notify_progress=notify_progress, keep=keep,
            )
            return {"job_id": job.id, "status": job.status, "kind": kind}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def add_torrent_file(
        torrent_base64: Annotated[str, Field(description="The .torrent file, base64-encoded.")],
        files: Annotated[list[int] | None, Field(description="Indexes of the files to download; default all.")] = None,
        name: Annotated[str | None, Field(description="A label for the job.")] = None,
        keep: bool = False,
        callback_url: str | None = None,
        callback_ref: str | None = None,
        notify_progress: bool = False,
    ) -> dict[str, Any]:
        """Start a torrent download from an uploaded .torrent file. Returns job_id and the torrent's file list."""
        try:
            check_callback(service, callback_url)
            service.torrent.ensure_session()
            try:
                data = base64.b64decode(torrent_base64, validate=False)
            except ValueError:
                raise JobError(JobError.INVALID_REQUEST, "torrent_base64 is not valid base64.") from None
            description = torrent.describe_torrent(torrent.parse_torrent(data))
            job = jobs.add(
                "torrent", f"torrent:{name or description['name']}", {"files": files} if files else {},
                callback_url=callback_url, callback_ref=callback_ref, notify_progress=notify_progress, keep=keep,
            )
            (service.env.state / f"{job.id}.torrent").write_bytes(data)
            return {"job_id": job.id, "status": job.status, "torrent": description}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def get_transcript(
        url: Annotated[str, Field(description="Video page URL.")],
        lang: Annotated[str, Field(description="Language code, e.g. en, vi, zh-Hans.")] = "en",
    ) -> dict[str, Any]:
        """Return a video's subtitles as plain text in the reply, without saving a file. Uploaded subtitles are
        preferred over auto-generated ones."""
        try:
            check_site(url, settings)
            return await service.video.transcript(url, lang)
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def list_jobs(
        status: Annotated[Literal["queued", "waiting", "downloading", "recording", "finalizing", "paused", "completed", "failed", "cancelled"] | None, Field(description="Only jobs in this state.")] = None,
    ) -> dict[str, Any]:
        """List jobs, newest first, with their progress."""
        return {"jobs": [service.describe_job(job) for job in jobs.by_status(status)]}

    @server.tool()
    async def get_job(id: Annotated[str, Field(description="The job_id.")]) -> dict[str, Any]:
        """A job's status, percent, speed, ETA, size, recorded time (live), error and output files with signed links."""
        try:
            return service.describe_job(jobs.job_by_id(id), with_links=True)
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def pause(id: str) -> dict[str, Any]:
        """Pause a queued or running job; partial files are kept for resume."""
        try:
            job = await jobs.pause(id)
            return {"job_id": job.id, "status": job.status}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def resume(id: str) -> dict[str, Any]:
        """Queue a paused (or failed) job again; it continues from its partial files."""
        try:
            job = jobs.resume(id)
            return {"job_id": job.id, "status": job.status}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def stop(id: str) -> dict[str, Any]:
        """End a live recording and keep it, merged to MP4."""
        try:
            job = await jobs.stop(id)
            return service.describe_job(job, with_links=True)
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def cancel(id: str) -> dict[str, Any]:
        """Stop a job and delete its partial and finished files."""
        try:
            job = await jobs.cancel(id)
            return {"job_id": job.id, "status": job.status}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def list_files(folder: Annotated[str | None, Field(description="A job's folder (its id) or a sub-folder.")] = None) -> dict[str, Any]:
        """Finished files: path, size, created time, deletion time (null when pinned with keep), pinned."""
        try:
            items = files.list_files(service.env.downloads, service.store, settings, folder or "")
            return {"files": items, "count": len(items)}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def delete_file(path: Annotated[str, Field(description="A path from list_files or get_job.")]) -> dict[str, Any]:
        """Delete a stored file (or a job's whole folder)."""
        try:
            target = files.resolve(service.env.downloads, path)
            if target == service.env.downloads.resolve():
                raise JobError(JobError.INVALID_REQUEST, "Refusing to delete the whole downloads folder.")
            relative = files.to_relative(service.env.downloads, target)
            if relative.split("/")[0] in jobs.running:
                raise JobError(JobError.INVALID_REQUEST, "That job is still running; cancel it instead.")
            if target.is_dir():
                shutil.rmtree(target)
                service.store.delete_files_under(relative)
            elif target.is_file():
                target.unlink()
                service.store.delete_file(relative)
            else:
                raise JobError(JobError.NOT_FOUND, f"No file {path}.")
            return {"deleted": relative}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def delete_all_files(
        confirm: Annotated[bool, Field(description="Must be true; deletes cannot be undone.")],
        include_pinned: Annotated[bool, Field(description="Also delete files kept with keep=true.")] = False,
    ) -> dict[str, Any]:
        """Delete every stored file. Running, queued and paused jobs are left alone (cancel them first); pinned
        files are kept unless include_pinned. Ask the user before calling this."""
        if not confirm:
            raise fail(JobError(JobError.INVALID_REQUEST, "Pass confirm=true to delete every stored file."))
        result = files.delete_all(service.env.downloads, service.store, jobs.busy_folders(), include_pinned)
        result["freed"] = files.gib(result["freed_bytes"])
        return result

    @server.tool()
    async def clip_file(
        path: Annotated[str, Field(description="A stored video's path.")],
        start: Annotated[str, Field(description="ss, mm:ss or hh:mm:ss.")],
        end: Annotated[str | None, Field(description="ss, mm:ss or hh:mm:ss; omit for the end.")] = None,
        precise: Annotated[bool | None, Field(description="Exact cut (re-encodes).")] = None,
        name: str | None = None,
        keep: bool = False,
        callback_url: str | None = None,
        callback_ref: str | None = None,
    ) -> dict[str, Any]:
        """Cut a stored video (any source, including torrent and eD2k downloads) into a new file. Returns a job_id."""
        try:
            check_callback(service, callback_url)
            source = files.resolve(service.env.downloads, path)
            if not source.is_file():
                raise JobError(JobError.NOT_FOUND, f"No file {path}.")
            from .media import parse_clips
            parse_clips([{"start": start, "end": end}])
            params = {"path": files.to_relative(service.env.downloads, source), "start": start, "end": end, "precise": precise, "name": name}
            job = jobs.add(
                "clip", f"file:{params['path']}", {k: v for k, v in params.items() if v is not None},
                callback_url=callback_url, callback_ref=callback_ref, keep=keep,
            )
            return {"job_id": job.id, "status": job.status}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def get_link(
        path: Annotated[str, Field(description="A stored file's path.")],
        expires_in: Annotated[float | None, Field(description="Seconds; default and maximum from settings.")] = None,
    ) -> dict[str, Any]:
        """A signed download URL for a stored file, and when it expires. A link never outlives its file."""
        try:
            return service.build_link(path, expires_in)
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def disk_usage() -> dict[str, Any]:
        """Bytes used by downloads, the quota, and the host's free space."""
        return service.disk.usage()

    @server.tool()
    async def set_cookies(
        site: Annotated[str, Field(description="youtube, instagram, facebook, tiktok, bilibili, or any domain.")],
        cookies_txt: Annotated[str, Field(description="A Netscape cookies.txt export for that site.")],
        browser: Annotated[Literal["chrome", "safari", "firefox", "edge"], Field(description="The browser the cookies came from; requests with them impersonate it.")] = "chrome",
    ) -> dict[str, Any]:
        """Save login cookies for a site. They are never returned by any tool."""
        try:
            saved = service.cookies.save(site, cookies_txt, browser)
            return {**saved, "saved": True}
        except JobError as error:
            raise fail(error) from None

    @server.tool()
    async def get_settings() -> dict[str, Any]:
        """The current limits and defaults (read-only; they are changed on the settings page)."""
        values = {setting.key: settings[setting.key] for setting in SETTINGS if setting.key != "proxy_url"}
        values["proxy_configured"] = bool(settings["proxy_url"])
        return {
            "settings": values,
            "running": len(jobs.running),
            "queued": service.store.count_jobs("queued"),
            "cookie_sites": [row["site"] for row in service.store.cookie_sites()],
            "webhooks_enabled": bool(service.env.webhook_secret),
            "versions": versions(),
        }

    return server
