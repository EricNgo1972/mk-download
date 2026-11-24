#!/usr/bin/env python3
"""mkd: manage mk-download's stored files from a terminal. Standard library only.

    export MKD_URL=https://download.maplekiosk.ca MCP_TOKEN=...
    mkd.py list                          numbered list of stored files
    mkd.py get_file_url <file> [hours]   signed download link (file = number from list, or path)
    mkd.py delete_file <file>...         delete files (numbers or paths; a job id deletes its folder)
    mkd.py delete_all [--include-pinned] [--yes]
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

USAGE = __doc__.split("\n\n", 1)[1]


def call(tool: str, arguments: dict | None = None) -> dict:
    base = os.environ.get("MKD_URL", "https://download.maplekiosk.ca").rstrip("/")
    token = os.environ.get("MCP_TOKEN")
    if not token:
        sys.exit("Set MCP_TOKEN (and MKD_URL if not https://download.maplekiosk.ca).")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments or {}}}
    request = urllib.request.Request(
        f"{base}/mcp", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"HTTP {error.code}: {error.read().decode(errors='replace')[:300]}")
    except urllib.error.URLError as error:
        sys.exit(f"Can't reach {base}: {error.reason}")
    if "error" in reply:
        sys.exit(f"RPC error: {reply['error']}")
    result = reply["result"]
    if result.get("isError"):
        text = result["content"][0]["text"]
        try:
            text = json.loads(text.split(": ", 1)[1] if text.startswith("Error") else text)["message"]
        except (ValueError, KeyError, IndexError):
            pass
        sys.exit(f"Error: {text}")
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def files() -> list[dict]:
    return call("list_files")["files"]


def resolve(ref: str) -> str:
    """A number from `list`, or a path / job id as is."""
    if ref.isdigit():
        items = files()
        index = int(ref)
        if not 1 <= index <= len(items):
            sys.exit(f"No file #{index}; `list` shows {len(items)}.")
        return items[index - 1]["path"]
    return ref


def cmd_list(args: list[str]) -> None:
    items = files()
    if not items:
        print("No stored files.")
        return
    for i, item in enumerate(items, 1):
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(item["created"]))
        expires = "pinned" if item["pinned"] else "deletes " + time.strftime("%m-%d", time.localtime(item["deletes_at"]))
        print(f"{i:>3}  {size(item['size']):>9}  {created}  {expires:<13}  {item['path']}")
    print(f"\n{len(items)} file(s), {size(sum(item['size'] for item in items))}")


def cmd_get_file_url(args: list[str]) -> None:
    if not args:
        sys.exit("usage: mkd.py get_file_url <number|path> [hours]")
    arguments = {"path": resolve(args[0])}
    if len(args) > 1:
        arguments["expires_in"] = float(args[1]) * 3600
    link = call("get_link", arguments)
    print(link["url"])
    print(f"expires {link.get('expires_at', '')}", file=sys.stderr)


def cmd_delete_file(args: list[str]) -> None:
    if not args:
        sys.exit("usage: mkd.py delete_file <number|path>...")
    paths = [resolve(ref) for ref in args]  # resolve all numbers first, so deletes don't shift them
    for path in paths:
        print("deleted", call("delete_file", {"path": path})["deleted"])


def cmd_delete_all(args: list[str]) -> None:
    include_pinned = "--include-pinned" in args
    if "--yes" not in args:
        items = files()
        count = sum(1 for item in items if include_pinned or not item["pinned"])
        answer = input(f"Delete {count} file(s){' including pinned' if include_pinned else ''}? Type yes: ")
        if answer.strip().lower() != "yes":
            sys.exit("Nothing deleted.")
    result = call("delete_all_files", {"confirm": True, "include_pinned": include_pinned})
    print(f"Deleted {result['deleted_files']} file(s), freed {size(result['freed_bytes'])}.")
    if result["kept_pinned"]:
        print(f"Kept {result['kept_pinned']} pinned file(s); add --include-pinned to delete them.")
    if result["skipped_active_jobs"]:
        print("Skipped active jobs:", ", ".join(result["skipped_active_jobs"]))


COMMANDS = {"list": cmd_list, "get_file_url": cmd_get_file_url, "delete_file": cmd_delete_file, "delete_all": cmd_delete_all}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(USAGE)
    COMMANDS[sys.argv[1]](sys.argv[2:])
