"""The one error type a job or a tool can end with: a machine-readable code and one plain sentence."""


class JobError(Exception):
    """Codes the agent and webhook callers react to. Keep the set small and stable; it is part of the API."""

    QUOTA_EXCEEDED = "quota_exceeded"
    LOGIN_REQUIRED = "login_required"
    BLOCKED_BY_CHALLENGE = "blocked_by_challenge"
    DRM_PROTECTED = "drm_protected"
    NOT_LIVE = "not_live"
    IS_LIVE = "is_live"
    UNSUPPORTED_URL = "unsupported_url"
    SOURCE_UNAVAILABLE = "source_unavailable"
    QUEUE_FULL = "queue_full"
    SITE_BLOCKED = "site_blocked"
    ENGINE_DISABLED = "engine_disabled"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    DOWNLOAD_FAILED = "download_failed"
    INTERNAL = "internal_error"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Halted(Exception):
    """Raised inside a yt-dlp hook to unwind a download the job manager interrupted (pause, cancel, shutdown)."""
