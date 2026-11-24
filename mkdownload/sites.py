"""Which site a URL belongs to: cookies per site, allowed/blocked sites, and the browser profile to impersonate."""

import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .errors import JobError
from .settings import Settings
from .store import Store

BROWSERS = ("chrome", "safari", "firefox", "edge")

# Short names an agent is likely to use, and hosts that belong to a bigger site.
SITE_NAMES = {
    "youtube": "youtube.com",
    "instagram": "instagram.com",
    "facebook": "facebook.com",
    "tiktok": "tiktok.com",
    "bilibili": "bilibili.com",
    "twitch": "twitch.tv",
    "twitter": "x.com",
    "x": "x.com",
}
HOST_ALIASES = {
    "youtu.be": "youtube.com",
    "b23.tv": "bilibili.com",
    "fb.watch": "facebook.com",
    "twitter.com": "x.com",
}
# Sites that block anything that does not look like a browser; auto mode always impersonates them.
ALWAYS_IMPERSONATE = ("tiktok.com", "instagram.com", "facebook.com")


def to_site(name: str) -> str:
    """A cookie site key: a bare domain such as youtube.com."""
    name = name.strip().lower()
    if "://" in name:
        name = urlsplit(name).hostname or ""
    name = name.removeprefix("www.").strip(".")
    name = SITE_NAMES.get(name, name)
    if not name or "/" in name or " " in name:
        raise JobError(JobError.INVALID_REQUEST, "site must be a domain such as youtube.com.")
    return HOST_ALIASES.get(name, name)


def host_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for alias, site in HOST_ALIASES.items():
        if host == alias or host.endswith("." + alias):
            return site
    return host


def match_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def check_site(url: str, settings: Settings) -> None:
    host = host_of(url)
    if not host:
        return
    if any(match_domain(host, domain) for domain in settings.lines("blocked_sites")):
        raise JobError(JobError.SITE_BLOCKED, f"{host} is on the blocked sites list.")
    allowed = settings.lines("allowed_sites")
    if allowed and not any(match_domain(host, domain) for domain in allowed):
        raise JobError(JobError.SITE_BLOCKED, f"{host} is not on the allowed sites list.")


class Cookies:
    def __init__(self, folder: Path, store: Store) -> None:
        self.folder = folder
        self.store = store

    def save(self, site: str, text: str, browser: str) -> dict:
        site = to_site(site)
        browser = (browser or "chrome").lower()
        if browser not in BROWSERS:
            raise JobError(JobError.INVALID_REQUEST, f"browser must be one of {', '.join(BROWSERS)}.")
        lines = [line for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
        rows = [line for line in lines if not line.startswith("#") or line.startswith("#HttpOnly_")]
        if not rows or any(len(line.split("\t")) != 7 for line in rows):
            raise JobError(
                JobError.INVALID_REQUEST,
                "cookies_txt must be a Netscape cookies.txt export: one cookie per line, 7 tab-separated fields.",
            )
        file = self.folder / f"{site}.txt"
        file.write_text("# Netscape HTTP Cookie File\n" + "\n".join(rows) + "\n")
        file.chmod(0o600)
        self.store.save_cookie_site(site, browser)
        return {"site": site, "browser": browser, "cookies": len(rows)}

    def delete(self, site: str) -> None:
        site = to_site(site)
        (self.folder / f"{site}.txt").unlink(missing_ok=True)
        self.store.delete_cookie_site(site)

    def match(self, url: str) -> tuple[Path, str] | None:
        """The cookie file and browser saved for the URL's site, most specific domain first."""
        host = host_of(url)
        rows = sorted(self.store.cookie_sites(), key=lambda row: len(row["site"]), reverse=True)
        for row in rows:
            if match_domain(host, row["site"]):
                file = self.folder / f"{row['site']}.txt"
                if file.is_file():
                    return file, row["browser"]
        return None

    def copy_for_job(self, url: str, folder: Path) -> tuple[Path | None, str | None]:
        """yt-dlp writes the jar back when it finishes; each job gets its own copy so jobs never race on it."""
        found = self.match(url)
        if found is None:
            return None, None
        file, browser = found
        copy = folder / ".cookies.txt"
        shutil.copyfile(file, copy)
        return copy, browser


def choose_impersonation(mode: str, url: str, cookie_browser: str | None) -> tuple[str | None, str | None]:
    """(target to use from the start, target to retry with once on HTTP 403/429)."""
    if mode == "off":
        return None, None
    if mode in BROWSERS:
        return mode, None
    host = host_of(url)
    if cookie_browser or any(match_domain(host, domain) for domain in ALWAYS_IMPERSONATE):
        return cookie_browser or "chrome", None
    return None, "chrome"
