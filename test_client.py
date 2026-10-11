#!/usr/bin/env python3
"""
test_client.py -- send real audio to a running stt_proxy.py (or any OpenAI-compatible
endpoint) and print the transcription.

    # against the proxy, using a generated sample clip:
    python3 test_client.py --url http://127.0.0.1:8765/v1/audio/transcriptions --make-sample

    # against the proxy with your own file:
    python3 test_client.py --url http://127.0.0.1:8765/v1/audio/transcriptions \
        --file voice-note.webm --model whisper-1 --language de

    # straight at OpenAI (bypasses the proxy, for comparison):
    python3 test_client.py --url https://api.openai.com/v1/audio/transcriptions \
        --file voice-note.m4a --api-key sk-...
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


def make_sample(path: Path, seconds: float = 3.0) -> Path:
    """Generate a webm/opus clip -- the format browsers hand you, which most
    STT backends refuse. Good proxy test input."""
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required to generate a sample")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000",
         "-f", "lavfi", "-i", "anoisesrc=d=3:color=pink",
         "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first[a]",
         "-map", "[a]", "-t", str(seconds), "-ac", "2", "-c:a", "libopus", str(path)],
        check=True, capture_output=True,
    )
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8765/v1/audio/transcriptions",
                    help="endpoint to POST to")
    ap.add_argument("--file", help="audio file to send; omit with --make-sample")
    ap.add_argument("--make-sample", action="store_true", help="generate a test webm clip")
    ap.add_argument("--model", default="whisper-1")
    ap.add_argument("--language", default=None)
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--api-key", default=None, help="Bearer token (default: $UPSTREAM_API_KEY)")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    import os

    tmp = None
    if args.file:
        path = Path(args.file)
        if not path.exists():
            sys.exit(f"no such file: {path}")
    elif args.make_sample:
        tmp = Path(tempfile.mkdtemp(prefix="stt-client-"))
        path = make_sample(tmp / "sample.webm")
        print(f"generated sample: {path} ({path.stat().st_size:,} bytes)")
    else:
        sys.exit("pass --file <audio> or --make-sample")

    data = {"model": args.model}
    if args.language:
        data["language"] = args.language
    if args.prompt:
        data["prompt"] = args.prompt

    headers = {}
    key = args.api_key or os.environ.get("UPSTREAM_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"

    with path.open("rb") as fh:
        started = time.monotonic()
        try:
            resp = requests.post(
                args.url,
                headers=headers,
                data=data,
                files={"file": (path.name, fh, "application/octet-stream")},
                timeout=args.timeout,
            )
        except requests.RequestException as exc:
            sys.exit(f"request failed: {exc}")
    elapsed = time.monotonic() - started

    print(f"POST {args.url}")
    print(f"  file  : {path.name} ({path.stat().st_size:,} bytes)")
    print(f"  fields: {data}")
    print(f"  -> {resp.status_code} in {elapsed:.2f}s")
    for h in ("X-Stt-Ffmpeg-Proxy", "X-Stt-Ffmpeg-Proxy-Converted", "Content-Type"):
        if h in resp.headers:
            print(f"  {h}: {resp.headers[h]}")

    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            payload = resp.json()
        except ValueError:
            print(resp.text)
            return 1
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if resp.ok and "text" in payload:
            print(f"\ntext: {payload['text']}")
    else:
        print(resp.text[:4000])
    if resp.status_code >= 400:
        print("\nerror response -- check the proxy log and the upstream credentials.", file=sys.stderr)
    return 0 if resp.ok else 1


if __name__ == "__main__":
    sys.exit(main())
