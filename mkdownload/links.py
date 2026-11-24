"""Expiring download links: /f/<token>/<file name>, where the token carries the path and expiry, HMAC-signed."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import PurePosixPath
from urllib.parse import quote


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, payload: str) -> str:
    return encode(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())


def build_token(secret: str, path: str, expires: int) -> str:
    payload = encode(json.dumps({"p": path, "e": expires}, separators=(",", ":")).encode())
    return f"{payload}.{sign(secret, payload)}"


def build_link(base_url: str, secret: str, path: str, expires: int) -> str:
    name = quote(PurePosixPath(path).name)
    return f"{base_url}/f/{build_token(secret, path, expires)}/{name}"


def parse_token(secret: str, token: str) -> str | None:
    """The path the token grants, or None when the signature is wrong or the link has expired."""
    payload, _, signature = token.partition(".")
    if not hmac.compare_digest(sign(secret, payload), signature):
        return None
    try:
        data = json.loads(decode(payload))
    except ValueError:
        return None
    if data["e"] < time.time():
        return None
    return data["p"]
