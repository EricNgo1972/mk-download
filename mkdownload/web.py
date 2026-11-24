"""HTTP entry points: /mcp (bearer token), /settings (admin password), /f/<token> (signed links), /healthz (open)."""

import asyncio
import hashlib
import hmac
import html
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from . import files, links
from .errors import JobError
from .service import Service, versions
from .settings import BY_KEY, SETTINGS, check_settings, parse_setting
from .sites import BROWSERS
from .tools import build_server

SESSION_COOKIE = "mk_session"
SESSION_SECONDS = 12 * 3600


class BearerGate:
    """Guards /mcp with MCP_TOKEN, and learns the public URL the agent reaches us on (for links)."""

    def __init__(self, app, service: Service) -> None:
        self.app = app
        self.service = service
        self.expected = f"Bearer {service.env.mcp_token}".encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope["headers"])
            if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
                response = JSONResponse(
                    {"error": "unauthorized", "message": "Send Authorization: Bearer <MCP_TOKEN>."},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="mk-download"'},
                )
                await response(scope, receive, send)
                return
            host = headers.get(b"x-forwarded-host") or headers.get(b"host")
            if host:
                self.service.learn_base_url(f"{scope.get('scheme', 'https')}://{host.decode()}")
        await self.app(scope, receive, send)


def build_app(service: Service) -> Starlette:
    mcp = build_server(service)
    # The tunnel delivers any Host; the bearer token is what guards /mcp.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    pages = Pages(service)

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            await service.start()
            try:
                yield
            finally:
                await service.close()

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def serve_file(request: Request) -> Response:
        path = links.parse_token(service.env.link_secret, request.path_params["token"])
        if path is None:
            return PlainTextResponse("This link is invalid or has expired.", status_code=403)
        try:
            file = files.resolve(service.env.downloads, path)
        except JobError:
            return PlainTextResponse("Not found.", status_code=404)
        if not file.is_file():
            return PlainTextResponse("The file has been deleted.", status_code=404)
        return FileResponse(file, filename=file.name, content_disposition_type="attachment")

    routes = [
        Route("/healthz", healthz),
        Route("/f/{token}", serve_file),
        Route("/f/{token}/{name:path}", serve_file),
        Route("/", lambda request: RedirectResponse("/settings")),
        Route("/settings", pages.show, methods=["GET"]),
        Route("/settings/login", pages.login, methods=["POST"]),
        Route("/settings/logout", pages.logout, methods=["POST"]),
        Route("/settings/save", pages.save, methods=["POST"]),
        Route("/settings/reset", pages.reset, methods=["POST"]),
        Route("/settings/cookies", pages.save_cookies, methods=["POST"]),
        Route("/settings/cookies/delete", pages.delete_cookies, methods=["POST"]),
        Route("/settings/webhooks/retry", pages.retry_webhook, methods=["POST"]),
        Mount("/", app=mcp_app),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(BearerGate, service=service)
    return app


class Pages:
    """The settings page: one server-rendered page, a login form in front of it, plain POST forms behind it."""

    def __init__(self, service: Service) -> None:
        self.service = service
        self.key = hashlib.sha256(f"session:{service.env.admin_password}:{service.env.link_secret}".encode()).digest()

    # session

    def sign(self, value: str) -> str:
        return hmac.new(self.key, value.encode(), hashlib.sha256).hexdigest()

    def session_of(self, request: Request) -> str | None:
        cookie = request.cookies.get(SESSION_COOKIE, "")
        value, _, signature = cookie.rpartition(".")
        if not value or not hmac.compare_digest(self.sign(value), signature):
            return None
        expires = value.split(":")[0]
        if not expires.isdigit() or int(expires) < time.time():
            return None
        return value

    def csrf_of(self, session: str) -> str:
        return self.sign("csrf:" + session)

    async def authorized_form(self, request: Request) -> tuple[str, Any] | None:
        session = self.session_of(request)
        if session is None:
            return None
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), self.csrf_of(session)):
            return None
        return session, form

    # routes

    async def login(self, request: Request) -> Response:
        form = await request.form()
        password = str(form.get("password", ""))
        if not hmac.compare_digest(password.encode(), self.service.env.admin_password.encode()):
            await asyncio.sleep(1.5)
            return HTMLResponse(render_login("Wrong password."), status_code=401)
        value = f"{int(time.time() + SESSION_SECONDS)}:{secrets.token_hex(8)}"
        response = RedirectResponse("/settings", status_code=303)
        response.set_cookie(
            SESSION_COOKIE, f"{value}.{self.sign(value)}", max_age=SESSION_SECONDS,
            httponly=True, secure=request.url.scheme == "https", samesite="strict",
        )
        return response

    async def logout(self, request: Request) -> Response:
        response = RedirectResponse("/settings", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    async def show(self, request: Request, message: str = "", problems: list[str] | None = None) -> Response:
        session = self.session_of(request)
        if session is None:
            return HTMLResponse(render_login())
        return HTMLResponse(self.render(self.csrf_of(session), message, problems or []))

    async def save(self, request: Request) -> Response:
        authorized = await self.authorized_form(request)
        if authorized is None:
            return RedirectResponse("/settings", status_code=303)
        _, form = authorized
        settings = self.service.settings
        proposed = dict(settings.values)
        problems = []
        for setting in SETTINGS:
            if setting.kind == "bool":
                proposed[setting.key] = setting.key in form
                continue
            if setting.key not in form:
                continue
            try:
                proposed[setting.key] = parse_setting(setting, str(form[setting.key]))
            except ValueError as error:
                problems.append(str(error))
        problems += check_settings(proposed, shutil.disk_usage(self.service.env.downloads).total)
        if problems:
            return await self.show(request, problems=problems)
        for key, value in proposed.items():
            if value != settings[key]:
                settings.set(key, value)
        self.service.jobs.wake.set()
        return await self.show(request, message="Saved. New values apply now.")

    async def reset(self, request: Request) -> Response:
        authorized = await self.authorized_form(request)
        if authorized is not None:
            key = str(authorized[1].get("key", ""))
            if key in BY_KEY:
                self.service.settings.reset(key)
                self.service.jobs.wake.set()
                return await self.show(request, message=f"{BY_KEY[key].label} reset to its default.")
        return RedirectResponse("/settings", status_code=303)

    async def save_cookies(self, request: Request) -> Response:
        authorized = await self.authorized_form(request)
        if authorized is None:
            return RedirectResponse("/settings", status_code=303)
        form = authorized[1]
        upload = form.get("file")
        text = str(form.get("text", ""))
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read()
            if data:
                text = data.decode(errors="replace")
        try:
            saved = self.service.cookies.save(str(form.get("site", "")), text, str(form.get("browser", "chrome")))
        except JobError as error:
            return await self.show(request, problems=[error.message])
        return await self.show(request, message=f"Saved {saved['cookies']} cookies for {saved['site']}.")

    async def delete_cookies(self, request: Request) -> Response:
        authorized = await self.authorized_form(request)
        if authorized is not None:
            site = str(authorized[1].get("site", ""))
            self.service.cookies.delete(site)
            return await self.show(request, message=f"Deleted the cookies for {site}.")
        return RedirectResponse("/settings", status_code=303)

    async def retry_webhook(self, request: Request) -> Response:
        authorized = await self.authorized_form(request)
        if authorized is not None:
            self.service.outbox.retry(str(authorized[1].get("id", "")))
            return await self.show(request, message="The webhook will be sent again.")
        return RedirectResponse("/settings", status_code=303)

    # rendering

    def render(self, csrf: str, message: str, problems: list[str]) -> str:
        service = self.service
        settings = service.settings
        usage = service.disk.usage()
        used, quota = usage["used_bytes"], usage["quota_bytes"]
        percent = min(100, used / quota * 100) if quota else 0
        hidden = f'<input type="hidden" name="csrf" value="{e(csrf)}">'
        notices = []
        if message:
            notices.append(f'<div class="note ok">{e(message)}</div>')
        for problem in problems:
            notices.append(f'<div class="note bad">{e(problem)}</div>')
        if settings["default_callback_url"] and not service.env.webhook_secret:
            notices.append('<div class="note bad">A default callback URL is set but WEBHOOK_SECRET is not, so no webhook is sent.</div>')
        if not service.base_url:
            notices.append('<div class="note warn">The public URL is unknown until the agent first calls /mcp (or set PUBLIC_URL); links can\'t be built yet.</div>')

        sections: dict[str, list[str]] = {}
        for setting in SETTINGS:
            sections.setdefault(setting.section, []).append(render_setting(setting, settings[setting.key], settings.defaults[setting.key], csrf))
        form_body = "".join(
            f'<fieldset><legend>{e(section)}</legend>{"".join(rows)}</fieldset>' for section, rows in sections.items()
        )

        cookie_rows = "".join(
            f'<tr><td>{e(row["site"])}</td><td>{e(row["browser"])}</td><td>{format_time(row["saved"])}</td>'
            f'<td><form method="post" action="/settings/cookies/delete">{hidden}'
            f'<input type="hidden" name="site" value="{e(row["site"])}"><button class="link danger">Delete</button></form></td></tr>'
            for row in service.store.cookie_sites()
        ) or '<tr><td colspan="4" class="muted">No cookies saved. Instagram and Facebook Reels need them.</td></tr>'
        browser_options = "".join(f'<option value="{b}">{b}</option>' for b in BROWSERS)

        undelivered = service.store.undelivered_events()
        webhook_rows = "".join(
            f'<tr><td><code>{e(row["job_id"])}</code></td><td>{e(row["type"])}</td><td class="wrap">{e(row["url"])}</td>'
            f'<td>{row["attempts"]}</td><td class="wrap">{e(row["last_error"] or "")}</td>'
            f'<td><form method="post" action="/settings/webhooks/retry">{hidden}<input type="hidden" name="id" value="{e(row["id"])}">'
            f'<button class="link">Retry</button></form></td></tr>'
            for row in undelivered
        ) or '<tr><td colspan="6" class="muted">None.</td></tr>'

        job_rows = "".join(
            f'<tr><td><code>{e(job.id)}</code></td><td>{e(job.kind)}{" · live" if job.is_live else ""}</td>'
            f'<td><span class="status s-{e(job.status)}">{e(job.status)}</span></td>'
            f'<td class="wrap">{e(job.progress.get("title") or job.url)}</td>'
            f'<td>{e(format_percent(job.progress.get("percent")))}</td>'
            f'<td class="wrap">{e(job.error_code or "")}</td></tr>'
            for job in service.jobs.by_status()[:25]
        ) or '<tr><td colspan="6" class="muted">No jobs yet.</td></tr>'

        version_text = " · ".join(f"{name} {value}" for name, value in versions().items())
        running = len(service.jobs.running)
        queued = service.store.count_jobs("queued")
        pending = service.store.count_events("pending")
        return PAGE.format(
            body=f"""
<header><h1>mk-download</h1><form method="post" action="/settings/logout">{hidden}<button class="link">Sign out</button></form></header>
{"".join(notices)}
<section class="stats">
  <div><span class="label">Disk used</span><span class="value">{files.gib(used)} of {files.gib(quota)}</span>
    <div class="bar"><div style="width:{percent:.1f}%"></div></div></div>
  <div><span class="label">Host free</span><span class="value">{files.gib(usage["host_free_bytes"])}</span></div>
  <div><span class="label">Jobs</span><span class="value">{running} running · {queued} queued</span></div>
  <div><span class="label">Webhooks</span><span class="value">{pending} pending · {len(undelivered)} undelivered</span></div>
</section>
<p class="muted small">{e(version_text)}</p>

<h2>Settings</h2>
<form method="post" action="/settings/save" class="settings">{hidden}{form_body}
<div class="actions"><button type="submit">Save settings</button></div></form>

<h2>Cookies</h2>
<p class="muted">Stored only on the server; no tool returns them. Requests with a site's cookies impersonate the browser they came from.</p>
<table><thead><tr><th>Site</th><th>Browser</th><th>Saved</th><th></th></tr></thead><tbody>{cookie_rows}</tbody></table>
<form method="post" action="/settings/cookies" enctype="multipart/form-data" class="cookies">{hidden}
  <label>Site <input name="site" placeholder="youtube.com" required></label>
  <label>Browser <select name="browser">{browser_options}</select></label>
  <label>cookies.txt file <input type="file" name="file" accept=".txt,text/plain"></label>
  <label class="full">or paste it <textarea name="text" rows="3" placeholder="# Netscape HTTP Cookie File"></textarea></label>
  <div class="actions"><button type="submit">Save cookies</button></div>
</form>

<h2>Undelivered webhooks</h2>
<table><thead><tr><th>Job</th><th>Event</th><th>URL</th><th>Tries</th><th>Last error</th><th></th></tr></thead><tbody>{webhook_rows}</tbody></table>

<h2>Recent jobs</h2>
<table><thead><tr><th>Job</th><th>Kind</th><th>Status</th><th>Title</th><th>Progress</th><th>Error</th></tr></thead><tbody>{job_rows}</tbody></table>
"""
        )


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def format_time(timestamp: float | None) -> str:
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC") if timestamp else ""


def format_percent(value: float | None) -> str:
    return f"{value:.1f}%" if isinstance(value, int | float) else ""


def render_setting(setting, value, default, csrf: str) -> str:
    name = setting.key
    unit = f'<span class="unit">{e(setting.unit)}</span>' if setting.unit else ""
    if setting.kind == "bool":
        control = f'<input type="checkbox" name="{name}" {"checked" if value else ""}>'
    elif setting.kind == "choice":
        options = "".join(f'<option value="{e(c)}" {"selected" if c == value else ""}>{e(c)}</option>' for c in setting.choices)
        control = f'<select name="{name}">{options}</select>'
    elif setting.kind == "lines":
        control = f'<textarea name="{name}" rows="2">{e(value)}</textarea>'
    elif setting.kind == "text":
        control = f'<input name="{name}" value="{e(value)}" class="wide">'
    else:
        step = "1" if setting.kind == "int" else "any"
        bounds = ""
        if setting.minimum is not None:
            bounds += f' min="{setting.minimum:g}"'
        if setting.maximum is not None:
            bounds += f' max="{setting.maximum:g}"'
        control = f'<input type="number" name="{name}" value="{value:g}" step="{step}"{bounds}>'
    shown_default = "on" if default is True else "off" if default is False else (default if default != "" else "empty")
    is_default = value == default
    reset = (
        "" if is_default else
        f'<button form="reset-{name}" class="link">Reset to default</button>'
        f'<form id="reset-{name}" method="post" action="/settings/reset" hidden>'
        f'<input type="hidden" name="csrf" value="{e(csrf)}"><input type="hidden" name="key" value="{name}"></form>'
    )
    help_text = f'<div class="help">{e(setting.help)}</div>' if setting.help else ""
    return (
        f'<div class="row"><label for="{name}">{e(setting.label)}</label>'
        f'<div class="control">{control}{unit}<span class="default">default {e(shown_default)}{e(" " + setting.unit if setting.unit and default != "" else "")}</span>{reset}</div>'
        f"{help_text}</div>"
    )


def render_login(error: str = "") -> str:
    note = f'<div class="note bad">{e(error)}</div>' if error else ""
    return PAGE.format(body=f"""
<div class="login"><h1>mk-download</h1><p class="muted">Settings are behind the admin password.</p>{note}
<form method="post" action="/settings/login"><input type="password" name="password" placeholder="Admin password" autofocus required>
<button type="submit">Sign in</button></form></div>""")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>mk-download settings</title>
<style>
:root {{ --bg:#f7f7f5; --panel:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e2e1dc; --accent:#2f6fde; --ok:#1f7a4d; --bad:#b3261e; --warn:#8a5a00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#161615; --panel:#1f1f1d; --text:#ecebe6; --muted:#9d9c96; --line:#33322f; --accent:#6f9df0; --ok:#5cc28e; --bad:#f08a82; --warn:#e0b45a; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:960px; margin:0 auto; padding:24px 16px 64px; }}
header {{ display:flex; justify-content:space-between; align-items:center; }}
h1 {{ font-size:22px; margin:0; }} h2 {{ font-size:17px; margin:32px 0 8px; }}
.muted {{ color:var(--muted); }} .small {{ font-size:12px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px; margin:16px 0 4px; }}
.stats > div {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }}
.label {{ display:block; color:var(--muted); font-size:12px; }} .value {{ font-weight:600; }}
.bar {{ height:6px; background:var(--line); border-radius:3px; margin-top:6px; overflow:hidden; }} .bar div {{ height:100%; background:var(--accent); }}
fieldset {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; margin:0 0 12px; padding:8px 14px 12px; }}
legend {{ font-weight:600; padding:0 4px; }}
.row {{ display:grid; grid-template-columns:minmax(160px,240px) 1fr; gap:4px 12px; padding:8px 0; border-top:1px solid var(--line); }}
.row:first-of-type {{ border-top:0; }}
.row label {{ font-weight:500; padding-top:4px; }} .help {{ grid-column:2; color:var(--muted); font-size:13px; }}
.control {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; }}
.default {{ color:var(--muted); font-size:12px; }} .unit {{ color:var(--muted); }}
input, select, textarea {{ font:inherit; color:var(--text); background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:5px 8px; }}
input[type=number] {{ width:110px; }} input.wide, textarea {{ width:100%; max-width:520px; }}
button {{ font:inherit; background:var(--accent); color:#fff; border:0; border-radius:6px; padding:7px 14px; cursor:pointer; }}
button.link {{ background:none; color:var(--accent); padding:0; font-size:13px; }} button.danger {{ color:var(--bad); }}
.actions {{ margin-top:8px; }}
table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); border-radius:8px; font-size:13px; }}
th, td {{ text-align:left; padding:6px 10px; border-top:1px solid var(--line); vertical-align:top; }} th {{ border-top:0; color:var(--muted); font-weight:500; }}
td.wrap {{ word-break:break-all; }}
.cookies {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:8px 12px; margin-top:10px; }}
.cookies label {{ display:flex; flex-direction:column; font-size:13px; color:var(--muted); }} .cookies .full {{ grid-column:1/-1; }}
.note {{ border-radius:6px; padding:8px 12px; margin:10px 0; border:1px solid; }}
.note.ok {{ color:var(--ok); }} .note.bad {{ color:var(--bad); }} .note.warn {{ color:var(--warn); }}
.status {{ font-weight:600; }} .s-completed {{ color:var(--ok); }} .s-failed {{ color:var(--bad); }}
.login {{ max-width:340px; margin:12vh auto; }} .login form {{ display:flex; gap:8px; margin-top:12px; }} .login input {{ flex:1; }}
@media (max-width:600px) {{ .row {{ grid-template-columns:1fr; }} .help {{ grid-column:1; }} table {{ display:block; overflow-x:auto; }} }}
</style></head><body><main>{body}</main></body></html>"""
