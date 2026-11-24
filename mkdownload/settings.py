"""Every limit and default, editable on /settings. Values live in SQLite and apply at once, without a restart."""

from dataclasses import dataclass
from typing import Any

from .store import Store

GB = 1024**3


@dataclass(frozen=True)
class Setting:
    key: str
    section: str
    label: str
    kind: str  # int, float, bool, choice, text, lines
    default: Any
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""


QUALITIES = ("best", "2160", "1440", "1080", "720", "480", "360")
IMPERSONATION = ("auto", "chrome", "safari", "firefox", "edge", "off")

SETTINGS = (
    Setting("max_downloads", "Jobs", "Downloads at once", "int", 3, minimum=1, maximum=20,
            help="Extra jobs wait as queued. Lowering it never stops running jobs."),
    Setting("max_live", "Jobs", "Live recordings at once", "int", 2, minimum=0, maximum=10,
            help="Separate slots, so long recordings don't block normal downloads."),
    Setting("max_queue", "Jobs", "Queue size limit", "int", 100, minimum=1, maximum=10000,
            help="New jobs beyond it fail with queue_full."),
    Setting("quota_gb", "Storage", "Disk quota", "float", 50, unit="GB", minimum=1,
            help="Lowering it deletes nothing; new downloads are refused until there is room."),
    Setting("min_free_percent", "Storage", "Minimum free disk on host", "float", 10, unit="%", minimum=0, maximum=90,
            help="Downloads stop even below the quota if the host runs this low."),
    Setting("cleanup_days", "Storage", "Delete finished files after", "float", 7, unit="days", minimum=0.04,
            help="keep: true exempts a file."),
    Setting("link_hours", "Links", "Default link expiry", "float", 24, unit="hours", minimum=0.1),
    Setting("public_files", "Links", "Public file list", "bool", True,
            help="Anyone can open /files without a password to see every stored file and download it."),
    Setting("link_max_days", "Links", "Maximum link expiry", "float", 7, unit="days", minimum=0.01, maximum=365),
    Setting("live_max_hours", "Live", "Default max_duration", "float", 4, unit="hours", minimum=0.01, maximum=48),
    Setting("live_wait_max_hours", "Live", "Maximum wait_for_start", "float", 12, unit="hours", minimum=0, maximum=168),
    Setting("default_quality", "Video", "Default quality", "choice", "best", choices=QUALITIES),
    Setting("sub_format", "Video", "Default subtitle format", "choice", "srt", choices=("srt", "vtt")),
    Setting("embed_subs", "Video", "Embed subtitles by default", "bool", False),
    Setting("precise_clips", "Video", "Precise clip cuts by default", "bool", False,
            help="Exact to the second but re-encodes, which is slower."),
    Setting("impersonate", "Network", "Impersonation mode", "choice", "auto", choices=IMPERSONATION,
            help="auto: always on for TikTok, Instagram and Facebook; elsewhere one retry as Chrome on 403/429."),
    Setting("speed_limit_kbps", "Network", "Speed limit", "int", 0, unit="KB/s", minimum=0,
            help="Per download; 0 is unlimited."),
    Setting("proxy_url", "Network", "Proxy URL", "text", "",
            help="For when the host's IP gets blocked, e.g. socks5://user:pass@host:1080. Empty goes direct."),
    Setting("seed_ratio", "Torrent / eD2k", "Seeding ratio limit", "float", 0, minimum=0, maximum=100,
            help="0 with a 0 time limit stops at completion."),
    Setting("seed_minutes", "Torrent / eD2k", "Seeding time limit", "int", 0, unit="minutes", minimum=0),
    Setting("max_peers", "Torrent / eD2k", "Max peer connections", "int", 200, minimum=10, maximum=2000),
    Setting("torrent_enabled", "Torrent / eD2k", "BitTorrent enabled", "bool", True),
    Setting("ed2k_enabled", "Torrent / eD2k", "eD2k enabled", "bool", True),
    Setting("allowed_sites", "Sites", "Allowed sites", "lines", "",
            help="One domain per line. When set, every other site is refused."),
    Setting("blocked_sites", "Sites", "Blocked sites", "lines", "", help="One domain per line."),
    Setting("default_callback_url", "Callbacks", "Default callback URL", "text", "",
            help="Used when a job gives no callback_url. Empty sends no webhook."),
)

BY_KEY = {setting.key: setting for setting in SETTINGS}


class Settings:
    def __init__(self, store: Store, proxy_default: str = "") -> None:
        self.store = store
        self.values: dict[str, Any] = {s.key: s.default for s in SETTINGS}
        self.values["proxy_url"] = proxy_default
        self.defaults = dict(self.values)
        for key in BY_KEY:
            saved = store.load_value(f"setting:{key}")
            if saved is not None:
                self.values[key] = saved

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def set(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.store.save_value(f"setting:{key}", value)

    def reset(self, key: str) -> None:
        self.values[key] = self.defaults[key]
        self.store.delete_value(f"setting:{key}")

    def lines(self, key: str) -> list[str]:
        return [line.strip().lower() for line in str(self.values[key]).splitlines() if line.strip()]

    @property
    def quota_bytes(self) -> int:
        return int(self.values["quota_gb"] * GB)


def parse_setting(setting: Setting, raw: str) -> Any:
    """Turns a form value into the stored value, or raises ValueError with a sentence for the page."""
    raw = raw.strip()
    match setting.kind:
        case "bool":
            return raw in ("on", "true", "1")
        case "int" | "float":
            try:
                value = int(raw) if setting.kind == "int" else float(raw)
            except ValueError:
                raise ValueError(f"{setting.label}: enter a number") from None
            if setting.minimum is not None and value < setting.minimum:
                raise ValueError(f"{setting.label}: at least {setting.minimum:g}")
            if setting.maximum is not None and value > setting.maximum:
                raise ValueError(f"{setting.label}: at most {setting.maximum:g}")
            return value
        case "choice":
            if raw not in setting.choices:
                raise ValueError(f"{setting.label}: choose one of {', '.join(setting.choices)}")
            return raw
        case "text":
            return raw
        case "lines":
            return "\n".join(line.strip() for line in raw.splitlines() if line.strip())
    raise ValueError(f"unknown setting kind {setting.kind}")


def check_settings(values: dict[str, Any], disk_total: int) -> list[str]:
    problems = []
    if values["quota_gb"] * GB > disk_total:
        problems.append(f"Disk quota can't exceed the disk ({disk_total / GB:.0f} GB)")
    if values["link_hours"] > values["link_max_days"] * 24:
        problems.append("Default link expiry can't exceed the maximum link expiry")
    proxy = values["proxy_url"]
    if proxy and "://" not in proxy:
        problems.append("Proxy URL needs a scheme, e.g. socks5://host:1080 or http://host:3128")
    return problems
