#!/usr/bin/env python3
"""
smoke_test.py -- end-to-end checks for stt_proxy.py, no API key or network needed.

Starts a fake upstream that records what it receives, points the proxy at it,
then posts real audio (generated with ffmpeg) and asserts on the result:
conversion shape, field passthrough, auth, error paths, upstream error relay.

    python3 smoke_test.py [-v]
"""

from __future__ import annotations

import argparse
import email
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROXY = HERE / "stt_proxy.py"

received: dict = {}
upstream_mode = {"status": 200}


class FakeUpstream(BaseHTTPRequestHandler):
    """Records the forwarded request and answers with a canned transcription."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        msg = email.message_from_bytes(
            f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + body
        )
        record = {"path": self.path, "fields": {}, "file": None,
                  "headers": {k.lower(): v for k, v in self.headers.items()}}
        for part in msg.walk():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                record["file"] = {
                    "field": name,
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "bytes": len(payload),
                    "magic": payload[:4].decode("latin1"),
                    "riff_tail": payload[8:12].decode("latin1"),
                }
            else:
                record["fields"][name] = payload.decode("utf-8", "replace")
        received.clear()
        received.update(record)

        if upstream_mode["status"] != 200:
            payload = json.dumps({"error": {"message": "boom", "type": "server_error"}}).encode()
        else:
            payload = json.dumps(
                {"text": "hello world", "language": "english", "duration": 1.0}
            ).encode()
        self.send_response(upstream_mode["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Upstream-Marker", "yes")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_a):  # silence
        pass


def check(name: str, ok: bool, detail: str = "") -> int:
    print(f"  {'PASS' if ok else 'FAIL'} {name:38s} {detail}")
    return 0 if ok else 1


def post(url: str, data: dict, timeout: int = 60):
    """Minimal multipart POST; returns (status, body_text)."""
    boundary = "----smoketestboundary1234"
    out = bytearray()
    for key, value in data.items():
        out += f"--{boundary}\r\n".encode()
        if isinstance(value, tuple):
            filename, content, ctype = value
            out += f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode()
            out += f"Content-Type: {ctype}\r\n\r\n".encode() + content + b"\r\n"
        else:
            out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
            out += str(value).encode() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=bytes(out),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": "Bearer client-token-should-be-forwarded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), dict(exc.headers)


def wait_for(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.15)
    return False


def make_audio(tmp: Path, cfg_ffmpeg: str = "ffmpeg") -> Path:
    """48 kHz stereo opus/webm -- the awkward case MediaRecorder produces."""
    src = tmp / "recording.webm"
    subprocess.run(
        [cfg_ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "2", "-ac", "2", "-c:a", "libopus", str(src)],
        check=True, capture_output=True,
    )
    return src


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        print("ffmpeg is required"); return 2
    if not PROXY.exists():
        print(f"missing {PROXY}"); return 2

    tmp = Path(tempfile.mkdtemp(prefix="smoke-"))
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    proxy_url = f"http://127.0.0.1:{args.port}"
    proc = subprocess.Popen(
        [sys.executable, str(PROXY),
         "--host", "127.0.0.1", "--port", str(args.port),
         "--upstream", f"http://127.0.0.1:{up_port}", "--path", "/v1/audio/transcriptions",
         "--max-upload-mb", "5"],
        stdout=subprocess.DEVNULL if not args.verbose else None,
        stderr=subprocess.DEVNULL if not args.verbose else None,
    )

    failures = 0
    try:
        if not wait_for(f"{proxy_url}/health"):
            print("FAIL proxy did not start"); proc.kill(); return 1
        audio = make_audio(tmp)
        raw = audio.read_bytes()

        print("health + conversion:")
        health = json.loads(urllib.request.urlopen(f"{proxy_url}/health", timeout=5).read())
        failures += check("GET /health", health.get("status") == "ok", health.get("ffmpeg") or "")
        failures += check("target is 16kHz mono", health.get("target") == "16000Hz/1ch/pcm_s16le",
                          health.get("target", ""))

        status, body, hdrs = post(
            f"{proxy_url}/v1/audio/transcriptions",
            {"file": ("recording.webm", raw, "audio/webm"),
             "model": "whisper-1", "language": "de", "response_format": "json",
             "prompt": "previous context", "temperature": "0"},
        )
        received_snapshot = dict(received)
        failures += check("POST transcriptions -> 200", status == 200, f"{status} {body[:60]}")
        failures += check("body relayed from upstream",
                          json.loads(body).get("text") == "hello world")
        failures += check("upstream header relayed", hdrs.get("X-Upstream-Marker") == "yes")
        failures += check("X-Stt-Ffmpeg-Proxy-Converted set", hdrs.get("X-Stt-Ffmpeg-Proxy-Converted") == "1")

        f = received_snapshot.get("file") or {}
        failures += check("file field named 'file'", f.get("field") == "file", str(f.get("field")))
        failures += check("filename rewritten to .wav", f.get("filename") == "recording.wav",
                          str(f.get("filename")))
        failures += check("content-type audio/wav", f.get("content_type") == "audio/wav",
                          str(f.get("content_type")))
        failures += check("payload is RIFF/WAVE",
                          f.get("magic") == "RIFF" and f.get("riff_tail") == "WAVE",
                          f"{f.get('magic')}/{f.get('riff_tail')}")
        # 2 s of 16 kHz mono s16le == 64000 bytes of payload (+ header)
        failures += check("2s webm -> 2s 16k mono pcm",
                          64_000 <= f.get("bytes", 0) <= 64_200,
                          f"{len(raw)}B opus -> {f.get('bytes')}B pcm")

        print("field + auth passthrough:")
        fields = received_snapshot.get("fields", {})
        failures += check("model forwarded", fields.get("model") == "whisper-1", str(fields.get("model")))
        failures += check("language forwarded", fields.get("language") == "de")
        failures += check("prompt forwarded", fields.get("prompt") == "previous context")
        failures += check("temperature forwarded", fields.get("temperature") == "0")
        failures += check("response_format forwarded", fields.get("response_format") == "json")
        failures += check("file field not duplicated in form", "file" not in fields)
        failures += check("client Authorization forwarded",
                          received_snapshot.get("headers", {}).get("authorization")
                          == "Bearer client-token-should-be-forwarded",
                          str(received_snapshot.get("headers", {}).get("authorization")))
        failures += check("upstream path correct", received_snapshot.get("path")
                          == "/v1/audio/transcriptions", str(received_snapshot.get("path")))

        print("error paths:")
        status, body, _ = post(f"{proxy_url}/v1/audio/transcriptions",
                               {"model": "whisper-1"})
        failures += check("missing file -> 400", status == 400, f"{status} {body[:50]}")

        req = urllib.request.Request(f"{proxy_url}/v1/audio/transcriptions",
                                     data=b'{"model":"whisper-1"}',
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
        failures += check("json body -> 415", status == 415, str(status))

        status, body, _ = post(f"{proxy_url}/v1/audio/transcriptions",
                               {"file": ("junk.txt", b"not audio at all", "text/plain"),
                                "model": "whisper-1"})
        failures += check("undecodable upload -> 422", status == 422, f"{status} {body[:60]}")

        big = tmp / "big.bin"
        big.write_bytes(b"\0" * (6 * 1024 * 1024))
        status, body, _ = post(f"{proxy_url}/v1/audio/transcriptions",
                               {"file": ("big.wav", big.read_bytes(), "audio/wav"),
                                "model": "whisper-1"})
        failures += check("oversize upload -> 413", status == 413, f"{status} {body[:50]}")

        upstream_mode["status"] = 429
        status, body, _ = post(f"{proxy_url}/v1/audio/transcriptions",
                               {"file": ("recording.webm", raw, "audio/webm"),
                                "model": "whisper-1"})
        failures += check("upstream 429 relayed", status == 429, f"{status} {body[:50]}")
        upstream_mode["status"] = 200

        try:
            urllib.request.urlopen(f"{proxy_url}/v1/audio/translations", timeout=5)
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
        failures += check("unknown route -> 404", status == 404, str(status))

        print("\nmock mode:")
        mock_port = args.port + 1
        mock = subprocess.Popen(
            [sys.executable, str(PROXY), "--host", "127.0.0.1", "--port", str(mock_port), "--mock"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            if not wait_for(f"http://127.0.0.1:{mock_port}/health"):
                failures += check("mock proxy start", False)
            else:
                status, body, _ = post(f"http://127.0.0.1:{mock_port}/v1/audio/transcriptions",
                                       {"file": ("recording.webm", raw, "audio/webm"),
                                        "model": "gpt-4o-transcribe"})
                info = json.loads(body).get("stt_proxy_mock", {}) if status == 200 else {}
                wav = info.get("forwarded_wav", {})
                failures += check("mock -> 200", status == 200, f"{status}")
                failures += check("mock reports 16000Hz", wav.get("sample_rate") == 16000,
                                  str(wav.get("sample_rate")))
                failures += check("mock reports mono", wav.get("channels") == 1, str(wav.get("channels")))
                failures += check("mock reports 16-bit", wav.get("bits_per_sample") == 16,
                                  str(wav.get("bits_per_sample")))
                failures += check("mock duration ~2s", 1.9 <= (wav.get("duration_s") or 0) <= 2.1,
                                  str(wav.get("duration_s")))
                failures += check("mock echoes fields",
                                  info.get("forwarded_fields", {}).get("model") == "gpt-4o-transcribe")
        finally:
            mock.terminate()
            mock.wait(timeout=10)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        upstream.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nsmoke test:", "ALL PASS" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
