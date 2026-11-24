# syntax=docker/dockerfile:1

# mk-download packaged as ONE MapleKiosk tenant container: an MCP-operated download service.
#
# One Python process serves /mcp (the agent), /settings (the admin page), /f/<token> (signed links) and /healthz,
# and drives three engines: yt-dlp + ffmpeg (video sites and streams), libtorrent (BitTorrent) and Kelpie (eD2k,
# a Go child process). Everything that must survive a redeploy lives on the /data volume.

# Deno is yt-dlp's JavaScript runtime: YouTube's player challenges are solved in JS.
FROM denoland/deno:bin-2.5.6 AS deno

FROM python:3.13-slim-trixie

ARG KELPIE_VERSION=v0.2.0
ARG KELPIE_SHA256=6890792c987c9f1a379f63bcf4163e88ff7341da3d1b8275e52ab601d6045918

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

# Kelpie's release binary, checked against the hash of the pinned release (linux-amd64; the fleet is x86).
ADD --chmod=755 https://github.com/XiaoYouChR/Kelpie/releases/download/${KELPIE_VERSION}/kelpie-linux-amd64 /opt/kelpie/kelpie
RUN echo "${KELPIE_SHA256}  /opt/kelpie/kelpie" | sha256sum -c -

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mkdownload ./mkdownload
COPY --chmod=755 mk-start.sh /usr/local/bin/mk-start.sh

# The service drops to this user in mk-start.sh, after making sure it owns /data.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

ENV DATA_DIR=/data \
    PORT=8000 \
    KELPIE_PATH=/opt/kelpie/kelpie \
    KELPIE_VERSION=${KELPIE_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 8000 is the only port the tunnel uses. The P2P ports matter only with HostNetwork (full speed, inbound peers).
EXPOSE 8000 6881/tcp 6881/udp 4662/tcp 4672/udp

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

ENTRYPOINT ["mk-start.sh"]
