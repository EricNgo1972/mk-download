# mk-download

A headless download service that an AI agent operates over MCP. It runs as one MapleKiosk tenant
(see [deploy/mk-provisioning.md](deploy/mk-provisioning.md)).

- **Video sites** through yt-dlp + ffmpeg: YouTube, TikTok, Instagram and Facebook Reels, Bilibili, ~1800 others,
  raw `.m3u8` / `.mpd` streams. Quality choice, audio only, clips, subtitles and transcripts, live recording.
- **BitTorrent** through libtorrent: magnet links, `.torrent` URLs and uploaded files, with file selection.
- **eD2k** through [Kelpie](https://github.com/XiaoYouChR/Kelpie) (Go engine, MIT; its Python control module is
  vendored in `mkdownload/kelpie/`).
- **Browser impersonation** (curl_cffi): TLS, HTTP/2 and header fingerprints of Chrome, Safari, Firefox or Edge.
- **Expiring signed links** to finished files, **signed webhooks** on completion, every limit on a **settings page**.

## Entry points

| Path | Who | Protection |
|---|---|---|
| `/mcp` | the agent (Streamable HTTP, stateless) | `Authorization: Bearer <MCP_TOKEN>` |
| `/settings` | you: settings, cookies, jobs, and a user guide (connecting an agent, MCP tools, HTTP API) | `ADMIN_PASSWORD`, session cookie, CSRF token |
| `/f/<token>/<name>` | anyone with the link | HMAC-signed path and expiry (`LINK_SECRET`) |
| `/healthz` | the provisioner | open; says only that the process is up |

## MCP tools

| Tool | Does |
|---|---|
| `probe(url)` | Title, length, qualities with sizes, subtitle languages, live state; torrent file list; eD2k name and size |
| `download(url, …)` | Starts a job, returns `job_id` at once. Options: `quality`, `audio_only`, `subtitles`, `auto_subs`, `sub_format`, `embed_subs`, `subtitles_only`, `clip`, `precise`, `files`, `live`, `max_duration`, `from_start`, `wait_for_start`, `impersonate`, `name`, `keep`, `callback_url`, `callback_ref`, `notify_progress` |
| `add_torrent_file(torrent_base64, …)` | Torrent job from an uploaded `.torrent` |
| `get_transcript(url, lang)` | Subtitle text in the reply, no file |
| `list_jobs(status?)`, `get_job(id)` | Status, percent, speed, ETA, recorded time, error, files with links |
| `pause`, `resume`, `stop`, `cancel` | `stop` ends a live recording and keeps it; `cancel` deletes the files |
| `list_files`, `delete_file`, `clip_file`, `get_link` | Stored files, cuts, signed links |
| `disk_usage`, `set_cookies`, `get_settings` | Quota, login cookies per site, read-only limits |

Job states: `queued` → `waiting` (scheduled live only) → `downloading` / `recording` → `finalizing` → `completed`;
or `paused`, `failed`, `cancelled`. Files land in `/data/downloads/<job_id>/`.

Tool errors are JSON: `{"error": "<code>", "message": "<one sentence>"}`, with codes `quota_exceeded`,
`login_required`, `blocked_by_challenge`, `drm_protected`, `not_live`, `is_live`, `unsupported_url`,
`source_unavailable`, `queue_full`, `site_blocked`, `engine_disabled`, `invalid_request`, `not_found`,
`download_failed`, `internal_error`.

## Webhooks

Pass `callback_url` (and optionally `callback_ref`, your own id) to `download`, `add_torrent_file` or `clip_file`,
or set a default on the settings page. When the job reaches `completed`, `failed` or `cancelled`, the service POSTs:

```json
{
  "event_id": "…", "type": "job.completed", "job_id": "20261124-ab12cd34", "callback_ref": "order-42",
  "status": "completed", "kind": "video", "url": "https://…",
  "files": [{"path": "20261124-ab12cd34/Title [id].mp4", "size": 12345, "link": "https://…/f/…", "link_expires_at": "…"}],
  "error": null, "progress": null, "created": "…", "finished": "…", "sent": "…"
}
```

- `X-MK-Signature: sha256=<hex>` is the HMAC-SHA256 of the raw body with `WEBHOOK_SECRET`. Verify it.
- The event is written to an outbox in the same SQLite transaction as the job's status, so a crash can't lose it.
- Any non-2xx reply or timeout is retried after 30 s, 2 min, 10 min, 1 h, then every 6 h, for 24 h; after that it
  shows as undelivered on the settings page, with a Retry button.
- Delivery is at-least-once: deduplicate on `event_id`.
- `notify_progress: true` adds `job.started` and `job.progress` (25 / 50 / 75 %) events.

## Live recording

`download(url, live=true)` records to 5-minute `.ts` segments under the job folder, so a crash or redeploy loses
nothing: after a restart the job continues and the segments are joined into one MP4 at the end. A dropped
connection reconnects. `stop`, the stream's end, `max_duration` (content time) and the disk quota all end the
recording and keep it. `wait_for_start` waits for a scheduled stream; `from_start` records a YouTube broadcast
from its beginning.

## Development

```bash
uv venv -p 3.13 .venv && uv pip install -p .venv -r requirements.txt   # needs ffmpeg on PATH; deno for YouTube
DATA_DIR=/tmp/mkd MCP_TOKEN=dev-token-0123456789 ADMIN_PASSWORD=dev-password-0123456 WEBHOOK_SECRET=dev \
  PORT=8765 .venv/bin/python -m mkdownload
MCP_TOKEN=dev-token-0123456789 tests/rpc.sh probe '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw"}'
```

`tests/smoke.py` is the end-to-end check the release workflow runs against the built image.

## Release

Actions → **Release Container** → *Run workflow* with a version (e.g. `1.0.0`). It builds the image, proves it
(refuses to start without secrets, engines present, a real download with clip, signed link and webhook, clean
stop), pushes `ghcr.io/ericngo1972/mk-download:<version>` and `:latest`, and prints the catalog row. Then set
the tenant's `ImageTag` and **Redeploy**.

Video sites change often. When downloads start failing with `download_failed` or `blocked_by_challenge`, bump
`yt-dlp` (and `curl_cffi` for newer browser fingerprints) in `requirements.in`, recompile `requirements.txt`, and
release.

## Licences

MIT. Kelpie and its control module: MIT, © XiaoYouChR (`mkdownload/kelpie/LICENSE.kelpie`). yt-dlp: Unlicense.
libtorrent: BSD. ffmpeg: from Debian, run as a separate program.
