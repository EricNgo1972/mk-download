"""End-to-end check of a running mk-download, standard library only.

    python3 tests/smoke.py BASE_URL MCP_TOKEN ADMIN_PASSWORD WEBHOOK_SECRET MEDIA_URL CALLBACK_HOST

MEDIA_URL serves test.mp4 (a 20 s video) to the service. A webhook receiver listens on port 18767 here, and the
service reaches it at http://CALLBACK_HOST:18767. Exits non-zero on the first failure.
"""

import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE, TOKEN, PASSWORD, SECRET, MEDIA, CALLBACK_HOST = sys.argv[1:7]
received: list[dict] = []


def fail(message: str) -> None:
    print(f"::error ::{message}")
    sys.exit(1)


def ok(message: str) -> None:
    print(f"  ok: {message}")


def http(method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def rpc(method: str, params: dict | None = None, token: str = TOKEN) -> tuple[int, dict]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode()
    status, data = http("POST", "/mcp", body, {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    return status, json.loads(data) if data else {}


def call(tool: str, **arguments) -> dict:
    status, reply = rpc("tools/call", {"name": tool, "arguments": arguments})
    if status != 200 or "error" in reply:
        fail(f"{tool}: HTTP {status} {reply}")
    result = reply["result"]
    if result.get("isError"):
        fail(f"{tool} failed: {result['content'][0]['text']}")
    return result["structuredContent"]


def wait_job(job_id: str, seconds: int = 120) -> dict:
    for _ in range(seconds):
        job = call("get_job", id=job_id)
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(1)
    fail(f"job {job_id} did not finish in {seconds} s (last status {job['status']})")


class Hook(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        received.append({"signed": self.headers.get("X-MK-Signature") == expected, "body": json.loads(body)})
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    threading.Thread(target=HTTPServer(("0.0.0.0", 18767), Hook).serve_forever, daemon=True).start()

    status, _ = rpc("tools/list", token="wrong-token")
    if status != 401:
        fail(f"/mcp answered a wrong token with HTTP {status}")
    ok("/mcp refuses a wrong token")

    _, reply = rpc("tools/list")
    names = sorted(tool["name"] for tool in reply["result"]["tools"])
    if len(names) != 19:
        fail(f"expected 19 tools, got {len(names)}: {names}")
    ok("19 MCP tools: " + ", ".join(names))

    probe = call("probe", url=MEDIA)
    if probe.get("type") != "video":
        fail(f"probe of the test video: {probe}")
    ok("probe reads a video")

    job = call("download", url=MEDIA, name="smoke", callback_url=f"http://{CALLBACK_HOST}:18767/hook", callback_ref="ci")
    job = wait_job(job["job_id"])
    if job["status"] != "completed" or not job["files"]:
        fail(f"download did not complete: {job}")
    file = job["files"][0]
    ok(f"download completed: {file['path']} ({file['size']} bytes)")

    status, data = http("GET", urllib.parse.urlsplit(file["link"]).path)
    if status != 200 or len(data) != file["size"]:
        fail(f"the signed link answered HTTP {status} with {len(data)} bytes")
    tampered = urllib.parse.urlsplit(file["link"]).path.replace("/f/e", "/f/f", 1)
    if http("GET", tampered)[0] != 403:
        fail("a tampered link was served")
    ok("signed link serves the file; a tampered one is refused")

    clip = wait_job(call("clip_file", path=file["path"], start="0:05", end="0:10")["job_id"])
    if clip["status"] != "completed":
        fail(f"clip_file failed: {clip}")
    ok(f"clip_file: {clip['files'][0]['path']}")

    for _ in range(30):
        if any(event["body"]["type"] == "job.completed" for event in received):
            break
        time.sleep(1)
    events = [event for event in received if event["body"]["type"] == "job.completed"]
    if not events:
        fail("no job.completed webhook arrived")
    if not all(event["signed"] for event in events):
        fail("a webhook carried a wrong X-MK-Signature")
    if events[0]["body"]["callback_ref"] != "ci" or not events[0]["body"]["files"][0]["link"]:
        fail(f"the webhook body is incomplete: {events[0]['body']}")
    ok("signed job.completed webhook with callback_ref and a link")

    error = rpc("tools/call", {"name": "get_link", "arguments": {"path": "../../etc/passwd"}})[1]["result"]
    if not error.get("isError") or "invalid_request" not in error["content"][0]["text"]:
        fail(f"a path outside downloads was not refused: {error}")
    ok("paths outside /data/downloads are refused")

    status, page = http("GET", "/settings")
    if status != 200 or b"Admin password" not in page:
        fail("/settings does not ask for the admin password")
    status, _ = http("POST", "/settings/login", b"password=wrong", {"Content-Type": "application/x-www-form-urlencoded"})
    if status != 401:
        fail(f"a wrong admin password answered HTTP {status}")
    ok("/settings is behind the admin password")

    call("delete_file", path=job["id"])
    if any(item["path"].startswith(job["id"]) for item in call("list_files")["files"]):
        fail("delete_file left files behind")
    ok("delete_file removes a job's folder")

    settings = call("get_settings")
    print("  versions:", settings["versions"])


if __name__ == "__main__":
    main()
