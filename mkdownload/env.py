"""The container's fixed configuration: secrets and paths from the environment. Tunable limits live in settings.py."""

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Env:
    mcp_token: str
    admin_password: str
    link_secret: str
    webhook_secret: str
    proxy_url: str
    # Optional. Links need an absolute URL; without it the service uses the host the agent last called /mcp on.
    public_url: str
    data: Path
    kelpie_path: Path
    bt_port: int
    ed2k_port: int

    @property
    def downloads(self) -> Path:
        return self.data / "downloads"

    @property
    def cookies(self) -> Path:
        return self.data / "cookies"

    @property
    def state(self) -> Path:
        return self.data / "state"

    @property
    def database(self) -> Path:
        return self.data / "mk-download.db"


def load_env() -> Env:
    data = Path(os.environ.get("DATA_DIR", "/data"))
    for folder in (data, data / "downloads", data / "cookies", data / "state", data / "ed2k"):
        folder.mkdir(parents=True, exist_ok=True)
    return Env(
        mcp_token=os.environ.get("MCP_TOKEN", ""),
        admin_password=os.environ.get("ADMIN_PASSWORD", ""),
        link_secret=os.environ.get("LINK_SECRET", "") or load_generated_secret(data / "state" / "link_secret"),
        webhook_secret=os.environ.get("WEBHOOK_SECRET", ""),
        proxy_url=os.environ.get("PROXY_URL", ""),
        public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
        data=data,
        kelpie_path=Path(os.environ.get("KELPIE_PATH", "/opt/kelpie/kelpie")),
        bt_port=int(os.environ.get("BT_PORT", "6881")),
        ed2k_port=int(os.environ.get("ED2K_PORT", "4662")),
    )


def load_generated_secret(file: Path) -> str:
    """LINK_SECRET is optional: one is generated on first start and kept on the volume, so links survive redeploys."""
    if file.is_file():
        return file.read_text().strip()
    value = secrets.token_hex(32)
    file.write_text(value)
    file.chmod(0o600)
    return value


def check_env(env: Env) -> list[str]:
    """Same rules as mk-start.sh, for running outside the container."""
    problems = []
    if len(env.mcp_token) < 16:
        problems.append("MCP_TOKEN is missing or shorter than 16 characters")
    if len(env.admin_password) < 16:
        problems.append("ADMIN_PASSWORD is missing or shorter than 16 characters")
    if env.mcp_token and env.mcp_token == env.admin_password:
        problems.append("MCP_TOKEN and ADMIN_PASSWORD must differ")
    return problems
