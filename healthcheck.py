#!/usr/bin/env python3
"""
healthcheck.py -- container health probe for openai-stt-ffmpeg-proxy.

Distroless has no shell, no curl and no wget, so the HEALTHCHECK has to be a
python script. Stdlib only: it must run even if the app's own dependencies are
broken, otherwise a bad deploy would report itself healthy.

Exit codes follow Docker's convention: 0 = healthy, 1 = unhealthy.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    host = os.environ.get("STT_PROXY_HEALTH_HOST", "127.0.0.1")
    port = os.environ.get("STT_PROXY_PORT", "8765")
    url = os.environ.get("STT_PROXY_HEALTH_URL", f"http://{host}:{port}/health")

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status != 200:
                print(f"unhealthy: /health returned {resp.status}", file=sys.stderr)
                return 1
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        print(f"unhealthy: /health returned {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # connection refused, timeout, ...
        print(f"unhealthy: {url} unreachable: {exc}", file=sys.stderr)
        return 1

    try:
        info = json.loads(body)
    except ValueError:
        print(f"unhealthy: /health was not JSON: {body[:120]!r}", file=sys.stderr)
        return 1

    if info.get("status") != "ok":
        print(f"unhealthy: status={info.get('status')!r}", file=sys.stderr)
        return 1
    # ffmpeg is the whole point of this proxy -- fail the container if it vanished.
    if not info.get("ffmpeg"):
        print("unhealthy: ffmpeg not found in the container", file=sys.stderr)
        return 1

    print(
        "healthy: ffmpeg=%s target=%s upstream=%s"
        % (info.get("ffmpeg"), info.get("target"), info.get("upstream"))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
