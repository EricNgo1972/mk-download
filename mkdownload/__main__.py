"""python -m mkdownload: checks the secrets, then serves everything on one port."""

import logging
import os
import sys

import uvicorn

from .env import check_env, load_env
from .service import Service
from .web import build_app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "mcp.server.streamable_http", "mcp.server.streamable_http_manager", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    env = load_env()
    problems = check_env(env)
    if problems:
        for problem in problems:
            logging.error("%s; refusing to start.", problem)
        sys.exit(64)
    app = build_app(Service(env))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="warning",
        timeout_graceful_shutdown=30,
    )


if __name__ == "__main__":
    main()
