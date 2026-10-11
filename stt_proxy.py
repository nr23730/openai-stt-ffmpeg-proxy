#!/usr/bin/env python3
"""
stt_proxy.py -- OpenAI-compatible STT proxy that re-encodes audio with ffmpeg.

Accepts POST /v1/audio/transcriptions (multipart/form-data, exactly like the
OpenAI API), decodes whatever container/codec the client sent (webm/ogg from
MediaRecorder, m4a/mp4, mp3, opus, aac, flac, amr, raw pcm, ...) through ffmpeg
into 16 kHz mono 16-bit PCM WAV, and forwards that WAV upstream together with
all the other form fields (model, language, prompt, temperature,
response_format, ...). The upstream reply is passed back verbatim.

Why: many STT backends are picky about containers/codecs, and a normalized WAV
is the one format every transcription model accepts.

Usage
-----
    export UPSTREAM_API_KEY=***            # or pass --api-key / forward client auth
    python3 stt_proxy.py --upstream https://api.openai.com

    # local smoke test, no upstream / no API key needed:
    python3 stt_proxy.py --selftest
    python3 stt_proxy.py --mock --port 8765   # echo back conversion info

Every option has an STT_PROXY_* environment equivalent (see --help).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterator

try:
    import requests
    from flask import Flask, Response, jsonify, request
except ImportError as exc:  # pragma: no cover
    sys.exit(f"missing dependency: {exc.name}. Run: pip install flask requests")

log = logging.getLogger("openai-stt-ffmpeg-proxy")

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

# Request headers forwarded upstream (case-insensitive). Auth comes from the
# client by default; --api-key overrides it.
FORWARD_REQ_HEADERS = (
    "authorization",
    "x-api-key",
    "openai-organization",
    "openai-project",
    "anthropic-version",
    "x-goog-api-key",
)

# Headers never copied in either direction.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-type",
    "host",
    "accept-encoding",
}

WAV_CODECS = {"pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_s16be", "pcm_s32le"}


def env(name: str, default: Any = None) -> Any:
    return os.environ.get(f"STT_PROXY_{name}", default)


@dataclass
class Config:
    listen_host: str = env("HOST", "127.0.0.1")
    listen_port: int = int(env("PORT", "8765"))
    upstream_base: str = env("UPSTREAM", "https://api.openai.com").rstrip("/")
    upstream_path: str = env("PATH", "/v1/audio/transcriptions")
    api_key: str | None = env("API_KEY")
    mount_path: str = env("MOUNT", "/v1/audio/transcriptions")

    ffmpeg_bin: str = env("FFMPEG", "ffmpeg")
    ffprobe_bin: str = env("FFPROBE", "ffprobe")
    sample_rate: int = int(env("SAMPLE_RATE", "16000"))
    channels: int = int(env("CHANNELS", "1"))
    codec: str = env("CODEC", "pcm_s16le")
    input_format: str | None = env("INPUT_FORMAT")  # force -f <fmt> on input
    probesize: str = env("PROBESIZE", "10M")
    ffmpeg_timeout: int = int(env("FFMPEG_TIMEOUT", "180"))

    max_upload_mb: float = float(env("MAX_UPLOAD_MB", "200"))
    upstream_timeout: int = int(env("UPSTREAM_TIMEOUT", "300"))
    threads: int = int(env("THREADS", "8"))
    server: str = env("SERVER", "auto")
    log_level: str = env("LOG_LEVEL", "info")
    passthrough_wav: bool = env("PASSTHROUGH_WAV", "0") == "1"
    save_dir: str | None = env("SAVE_DIR")
    forward_headers: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip().lower()
            for h in (env("FORWARD_HEADERS", "") or "").split(",")
            if h.strip()
        )
        + FORWARD_REQ_HEADERS
    )

    mock: bool = env("MOCK", "0") == "1"
    keep_tmp: bool = env("KEEP_TMP", "0") == "1"

    @property
    def upstream_url(self) -> str:
        path = self.upstream_path
        return self.upstream_base if re.match(r"^https?://", path) else f"{self.upstream_base}{path}"


class ProxyError(Exception):
    """Client-facing error with an HTTP status."""

    def __init__(self, status: int, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------
# media helpers
# --------------------------------------------------------------------------

def ffprobe_info(path: Path, cfg: Config) -> dict[str, Any]:
    """Best-effort media probe. Never raises -- returns {} if unavailable."""
    exe = shutil.which(cfg.ffprobe_bin)
    if not exe:
        return {}
    try:
        out = subprocess.run(
            [
                exe, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if out.returncode != 0:
            return {}
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        log.debug("ffprobe failed: %s", exc)
        return {}

    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or audio.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "format": fmt.get("format_name"),
        "codec": audio.get("codec_name"),
        "sample_rate": int(audio["sample_rate"]) if str(audio.get("sample_rate", "")).isdigit() else None,
        "channels": int(audio["channels"]) if str(audio.get("channels", "")).isdigit() else None,
        "duration_s": round(duration, 3),
    }


def can_skip_conversion(info: dict[str, Any], cfg: Config) -> bool:
    """True when the input is already the exact WAV shape we would produce."""
    return (
        cfg.passthrough_wav
        and "wav" in (info.get("format") or "")
        and info.get("codec") in WAV_CODECS
        and info.get("sample_rate") == cfg.sample_rate
        and info.get("channels") == cfg.channels
    )


def build_ffmpeg_cmd(src: Path, dst: Path, cfg: Config) -> list[str]:
    cmd = [
        cfg.ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "error",
        "-nostdin",
        "-y",
    ]
    if cfg.input_format:
        cmd += ["-f", cfg.input_format]
    cmd += [
        "-probesize", cfg.probesize,
        "-i", str(src),
        "-map", "0:a:0",          # first audio stream only
        "-ac", str(cfg.channels),  # down/up-mix
        "-ar", str(cfg.sample_rate),
        "-c:a", cfg.codec,
        "-f", "wav",
        str(dst),
    ]
    return cmd


def convert_to_wav(src: Path, cfg: Config) -> Path:
    """Run ffmpeg: any input file -> normalized WAV next to it. Raises ProxyError."""
    dst = src.with_name(src.stem + ".converted.wav")
    cmd = build_ffmpeg_cmd(src, dst, cfg)
    log.debug("exec: %s", " ".join(cmd))

    if not shutil.which(cfg.ffmpeg_bin):
        dst.unlink(missing_ok=True)
        raise ProxyError(500, f"ffmpeg binary not found: {cfg.ffmpeg_bin}")

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=cfg.ffmpeg_timeout,
        )
    except subprocess.TimeoutExpired:
        dst.unlink(missing_ok=True)
        raise ProxyError(
            504, f"ffmpeg timed out after {cfg.ffmpeg_timeout}s", ffmpeg_timeout=cfg.ffmpeg_timeout
        )

    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size <= 44:
        dst.unlink(missing_ok=True)
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        raise ProxyError(
            422,
            "ffmpeg could not decode the uploaded audio",
            ffmpeg_exit=proc.returncode,
            ffmpeg_stderr=tail,
        )
    return dst


def save_upload(file_storage, dest: Path, limit_bytes: int) -> int:
    """Stream the multipart part to disk with a hard size cap. Returns bytes."""
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = file_storage.stream.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > limit_bytes:
                raise ProxyError(413, f"upload exceeds {limit_bytes / 1e6:.0f} MB limit")
            out.write(chunk)
    if size == 0:
        raise ProxyError(400, "uploaded file is empty")
    return size


def wav_header(path: Path) -> dict[str, Any]:
    """Walk the RIFF chunk list and return the fmt facts (logging + mock reply)."""
    import struct

    out: dict[str, Any] = {"valid_wav": False}
    try:
        with path.open("rb") as fh:
            head = fh.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return out
            out["valid_wav"] = True

            byte_rate = 0
            data_size = max(path.stat().st_size, 12) - 12
            while True:
                chunk_head = fh.read(8)
                if len(chunk_head) < 8:
                    break
                cid, size = chunk_head[:4], struct.unpack("<I", chunk_head[4:8])[0]
                if cid == b"fmt ":
                    audio_format, channels, rate, byte_rate, _blk, bits = struct.unpack(
                        "<HHIIHH", fh.read(size)[:16]
                    )
                    out.update(
                        audio_format=audio_format,
                        channels=channels,
                        sample_rate=rate,
                        bits_per_sample=bits,
                    )
                elif cid == b"data":
                    data_size = size
                    break  # nothing useful after the audio payload
                fh.seek(size + (size % 2), os.SEEK_CUR)  # chunks are word-aligned

            out["data_bytes"] = data_size
            if byte_rate:
                out["duration_s"] = round(data_size / byte_rate, 3)
    except (OSError, struct.error) as exc:
        log.debug("wav header parse failed for %s: %s", path, exc)
    return out


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------

class TranscriptionProxy:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.app = Flask(__name__)
        self.app.url_map.strict_slashes = False
        self._register()

    # -- plumbing ----------------------------------------------------------

    def _register(self) -> None:
        cfg = self.cfg
        self.app.add_url_rule(cfg.mount_path, "transcribe", self.transcribe, methods=["POST"])
        self.app.add_url_rule("/health", "health", self.health, methods=["GET"])
        self.app.add_url_rule("/healthz", "healthz", self.health, methods=["GET"])

        @self.app.errorhandler(ProxyError)
        def _proxy_error(err: ProxyError) -> Response:
            body = {"error": {"message": err.message, "type": "stt_proxy_error", **err.extra}}
            return Response(json.dumps(body), status=err.status, content_type="application/json")

        @self.app.errorhandler(404)
        def _not_found(_e: Any) -> Response:
            body = {
                "error": {
                    "message": f"unknown route {request.method} {request.path}; "
                               f"this proxy serves POST {cfg.mount_path}",
                    "type": "stt_proxy_error",
                }
            }
            return Response(json.dumps(body), status=404, content_type="application/json")

        @self.app.after_request
        def _hdrs(resp: Response) -> Response:
            resp.headers["X-Stt-Ffmpeg-Proxy"] = "ffmpeg"
            return resp

    def health(self) -> Response:
        info: dict[str, Any] = {
            "status": "ok",
            "ffmpeg": shutil.which(self.cfg.ffmpeg_bin),
            "ffprobe": shutil.which(self.cfg.ffprobe_bin),
            "upstream": self.cfg.upstream_url,
            "mock": self.cfg.mock,
            "target": f"{self.cfg.sample_rate}Hz/{self.cfg.channels}ch/{self.cfg.codec}",
        }
        return Response(json.dumps(info, indent=2) + "\n", content_type="application/json")

    # -- main handler ------------------------------------------------------

    def transcribe(self) -> Response:
        cfg = self.cfg
        started = time.monotonic()
        req_id = uuid.uuid4().hex[:6]

        if "multipart/form-data" not in (request.content_type or "").lower():
            raise ProxyError(
                415,
                "expected multipart/form-data with a 'file' field "
                "(OpenAI /v1/audio/transcriptions format)",
            )

        file_storage = request.files.get("file") or next(iter(request.files.values()), None)
        if file_storage is None or not file_storage.filename:
            raise ProxyError(400, "missing multipart field 'file'")

        fields = {k: v for k, v in request.form.items() if k != "file"}
        original_name = Path(file_storage.filename).name
        limit = int(cfg.max_upload_mb * 1024 * 1024)

        tmpdir = Path(tempfile.mkdtemp(prefix="openai-stt-ffmpeg-proxy-"))
        try:
            src = tmpdir / ("in" + Path(original_name).suffix.lower())
            in_bytes = save_upload(file_storage, src, limit)
            src_info = ffprobe_info(src, cfg)
            log.info(
                "[%s] recv %s (%s, %s bytes) -> %s",
                req_id, original_name, src_info.get("format") or "?", f"{in_bytes:,}",
                cfg.upstream_url,
            )

            if can_skip_conversion(src_info, cfg):
                wav, converted = src, False
                log.info("[%s] already %dHz/%sch WAV, ffmpeg skipped",
                         req_id, src_info["sample_rate"], src_info["channels"])
            else:
                wav = convert_to_wav(src, cfg)
                converted = True

            wav_bytes = wav.stat().st_size
            wav_info = wav_header(wav)
            log.info(
                "[%s] %s -> wav %s bytes @ %sHz/%sch (%.1fs audio) in %.2fs",
                req_id, f"{in_bytes:,}", f"{wav_bytes:,}",
                wav_info.get("sample_rate"), wav_info.get("channels"),
                wav_info.get("duration_s", 0.0), time.monotonic() - started,
            )

            if cfg.save_dir:
                self._archive(wav, original_name, req_id)

            if cfg.mock:
                return self._mock_reply(wav, original_name, fields, src_info, in_bytes, req_id)

            return self._forward(wav, original_name, fields, req_id, converted, started)
        finally:
            if cfg.keep_tmp:
                log.info("[%s] keeping temp dir %s", req_id, tmpdir)
            else:
                shutil.rmtree(tmpdir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    def _archive(self, wav: Path, original_name: str, req_id: str) -> None:
        cfg_path = self.cfg.save_dir or ""
        try:
            dest_dir = Path(cfg_path).expanduser()
            dest_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = dest_dir / f"{stamp}-{req_id}-{Path(original_name).stem}.wav"
            shutil.copyfile(wav, dest)
            log.info("[%s] archived converted audio -> %s", req_id, dest)
        except OSError as exc:
            log.warning("[%s] could not archive to %s: %s", req_id, cfg_path, exc)

    def _out_headers(self) -> dict[str, str]:
        cfg = self.cfg
        headers: dict[str, str] = {}
        for name in cfg.forward_headers:
            value = request.headers.get(name)
            if value:
                headers[name] = value
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        headers["Accept"] = "application/json"
        headers["User-Agent"] = "openai-stt-ffmpeg-proxy/1.0 (+ffmpeg)"
        return headers

    def _forward(self, wav: Path, original_name: str, fields: dict[str, str],
                 req_id: str, converted: bool, started: float) -> Response:
        cfg = self.cfg
        out_name = Path(original_name).with_suffix("").name or "audio"
        out_name = f"{out_name or 'audio'}.wav"

        with wav.open("rb") as fh:
            fwd_started = time.monotonic()
            try:
                upstream = requests.post(
                    cfg.upstream_url,
                    headers=self._out_headers(),
                    data=fields,
                    files={"file": (out_name, fh, "audio/wav")},
                    timeout=cfg.upstream_timeout,
                    stream=True,
                    allow_redirects=False,
                )
            except requests.Timeout:
                raise ProxyError(504, f"upstream timed out after {cfg.upstream_timeout}s") from None
            except requests.RequestException as exc:
                log.error("[%s] upstream error: %s", req_id, exc)
                raise ProxyError(502, f"upstream request failed: {exc.__class__.__name__}: {exc}") from None

            log.info(
                "[%s] upstream %d in %.2fs (total %.2fs, sent %s of %s bytes, fields=%s)",
                req_id, upstream.status_code, time.monotonic() - fwd_started,
                time.monotonic() - started, out_name,
                f"{wav.stat().st_size:,}", sorted(fields) or "-",
            )
            headers = {
                k: v for k, v in upstream.headers.items()
                if k.lower() not in HOP_BY_HOP and k.lower() not in ("content-encoding", "set-cookie")
            }
            headers["X-Stt-Ffmpeg-Proxy-Converted"] = "1" if converted else "0"
            return Response(
                _stream(upstream),
                status=upstream.status_code,
                headers=headers,
                content_type=upstream.headers.get("Content-Type", "application/json"),
                direct_passthrough=False,
            )

    def _mock_reply(self, wav: Path, original_name: str, fields: dict[str, str],
                    src_info: dict[str, Any], in_bytes: int, req_id: str) -> Response:
        body = {
            "text": "[mock] this proxy did not call any upstream",
            "task": None,
            "stt_proxy_mock": {
                "received_name": original_name,
                "received_bytes": in_bytes,
                "received_probe": src_info,
                "forwarded_name": f"{Path(original_name).with_suffix('').name or 'audio'}.wav",
                "forwarded_bytes": wav.stat().st_size,
                "forwarded_wav": wav_header(wav),
                "forwarded_fields": fields,
                "would_post_to": self.cfg.upstream_url,
            },
        }
        return Response(json.dumps(body, indent=2) + "\n", content_type="application/json")


def _stream(upstream: "requests.Response") -> Iterator[bytes]:
    try:
        yield from upstream.iter_content(chunk_size=1 << 16)
    finally:
        upstream.close()


# --------------------------------------------------------------------------
# self test
# --------------------------------------------------------------------------

def selftest(cfg: Config) -> int:
    """Generate odd-format audio with ffmpeg, convert it, verify the WAV. Offline."""
    print(f"selftest: ffmpeg={shutil.which(cfg.ffmpeg_bin)} ffprobe={shutil.which(cfg.ffprobe_bin)}")
    if not shutil.which(cfg.ffmpeg_bin):
        print("FAIL: ffmpeg not on PATH")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="stt-ffmpeg-selftest-"))
    failures = 0
    cases = [
        ("sine.mp3", ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "1.5"]),
        ("voice.webm", ["-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000", "-t", "1",
                        "-c:a", "libopus", "-ac", "2"]),
        ("clip.m4a", ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=24000", "-t", "1",
                      "-c:a", "aac"]),
        ("raw.wav", ["-f", "lavfi", "-i", "sine=frequency=100:sample_rate=8000", "-t", "0.5"]),
        ("stereo.flac", ["-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000", "-t", "1",
                         "-c:a", "flac", "-ac", "2"]),
        ("video.mp4", ["-f", "lavfi", "-i", "testsrc=size=64x64:rate=10", "-t", "0.2"]),
    ]
    must_fail = "video.mp4"  # no audio stream -> must be rejected

    try:
        for name, extra in cases:
            src = tmp / name
            gen = [cfg.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *extra, str(src)]
            proc = subprocess.run(gen, capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"  SKIP {name:15s} (generator unavailable: {proc.stderr.strip()[:60]})")
                continue
            probe = ffprobe_info(src, cfg)
            try:
                wav = convert_to_wav(src, cfg)
                info = wav_header(wav)
                ok = (
                    info.get("valid_wav")
                    and info.get("sample_rate") == cfg.sample_rate
                    and info.get("channels") == cfg.channels
                )
                if name == must_fail:
                    print(f"  FAIL {name:15s} expected rejection, got a WAV")
                    failures += 1
                elif ok:
                    print(f"  PASS {name:15s} {probe.get('codec')}/{probe.get('sample_rate')}Hz"
                          f"/{probe.get('channels')}ch -> "
                          f"{info['sample_rate']}Hz/{info['channels']}ch/{info['bits_per_sample']}bit"
                          f" {wav.stat().st_size}B ({info.get('duration_s')}s)")
                else:
                    print(f"  FAIL {name:15s} unexpected header {info}")
                    failures += 1
            except ProxyError as exc:
                if name == must_fail:
                    print(f"  PASS {name:15s} rejected as expected ({exc.status}: {exc.message})")
                else:
                    print(f"  FAIL {name:15s} {exc.message} {exc.extra}")
                    failures += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("selftest:", "OK" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    d = Config()
    p = argparse.ArgumentParser(
        prog="stt_proxy.py",
        description="Proxy /v1/audio/transcriptions, re-encoding audio to WAV with ffmpeg.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host", default=d.listen_host, help="bind address")
    p.add_argument("--port", "-p", type=int, default=d.listen_port, help="bind port")
    p.add_argument("--upstream", default=d.upstream_base,
                   help="upstream base URL (or full URL when --path is empty)")
    p.add_argument("--path", default=d.upstream_path, help="upstream path appended to --upstream")
    p.add_argument("--mount", default=d.mount_path, help="path this proxy listens on")
    p.add_argument("--api-key", default=d.api_key,
                   help="bearer token for upstream (default: pass through the client's Authorization)")
    p.add_argument("--sample-rate", type=int, default=d.sample_rate, help="target WAV sample rate")
    p.add_argument("--channels", type=int, default=d.channels, help="target channels (1 = mono)")
    p.add_argument("--codec", default=d.codec, help="WAV PCM codec, e.g. pcm_s16le / pcm_f32le")
    p.add_argument("--input-format", default=d.input_format,
                   help="force ffmpeg input demuxer (webm, ogg, mp4, ...) when probing fails")
    p.add_argument("--ffmpeg", default=d.ffmpeg_bin, help="ffmpeg executable")
    p.add_argument("--ffprobe", default=d.ffprobe_bin, help="ffprobe executable")
    p.add_argument("--ffmpeg-timeout", type=int, default=d.ffmpeg_timeout, help="ffmpeg timeout (s)")
    p.add_argument("--upstream-timeout", type=int, default=d.upstream_timeout, help="upstream timeout (s)")
    p.add_argument("--max-upload-mb", type=float, default=d.max_upload_mb, help="max upload size (MiB)")
    p.add_argument("--passthrough-wav", action="store_true", default=d.passthrough_wav,
                   help="skip ffmpeg when input already matches the target WAV shape")
    p.add_argument("--save-dir", default=d.save_dir, help="also keep each converted WAV here")
    p.add_argument("--forward-header", action="append", default=[], dest="extra_headers",
                   help="extra client header to forward upstream (repeatable)")
    p.add_argument("--mock", action="store_true", default=d.mock,
                   help="do not call upstream; return conversion details as JSON")
    p.add_argument("--server", choices=("auto", "waitress", "flask"), default=d.server,
                   help="WSGI server: waitress if installed, else Flask dev server")
    p.add_argument("--threads", type=int, default=d.threads, help="worker threads (waitress)")
    p.add_argument("--keep-tmp", action="store_true", default=d.keep_tmp, help="keep temp files")
    p.add_argument("--selftest", action="store_true", help="run offline ffmpeg checks and exit")
    p.add_argument("--log-level", choices=("debug", "info", "warning", "error"), default=d.log_level,
                   help="log verbosity (env STT_PROXY_LOG_LEVEL)")
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging (same as --log-level debug)")
    return p.parse_args(argv)


def config_from_args(args: argparse.Namespace) -> Config:
    cfg = Config()
    cfg.listen_host, cfg.listen_port = args.host, args.port
    cfg.upstream_base = args.upstream.rstrip("/")
    cfg.upstream_path = args.path
    cfg.mount_path = args.mount
    cfg.api_key = args.api_key
    cfg.sample_rate, cfg.channels, cfg.codec = args.sample_rate, args.channels, args.codec
    cfg.input_format = args.input_format
    cfg.ffmpeg_bin, cfg.ffprobe_bin = args.ffmpeg, args.ffprobe
    cfg.ffmpeg_timeout, cfg.upstream_timeout = args.ffmpeg_timeout, args.upstream_timeout
    cfg.max_upload_mb = args.max_upload_mb
    cfg.passthrough_wav = args.passthrough_wav
    cfg.save_dir = args.save_dir
    cfg.forward_headers = tuple(h.lower() for h in args.extra_headers) + FORWARD_REQ_HEADERS
    cfg.mock, cfg.keep_tmp = args.mock, args.keep_tmp
    cfg.server, cfg.threads = args.server, args.threads
    cfg.log_level = "debug" if args.verbose else args.log_level
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = config_from_args(args)
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    if args.selftest:
        return selftest(cfg)

    if not shutil.which(cfg.ffmpeg_bin):
        log.error("ffmpeg not found (%s); install it or pass --ffmpeg", cfg.ffmpeg_bin)
        return 2
    if not cfg.mock:
        log.info("auth source: %s", "--api-key" if cfg.api_key
                 else "client Authorization header (pass-through)")

    app = TranscriptionProxy(cfg).app
    log.info("openai-stt-ffmpeg-proxy listening on http://%s:%d%s", cfg.listen_host, cfg.listen_port, cfg.mount_path)
    log.info("  upstream : %s%s", cfg.upstream_base, cfg.upstream_path)
    log.info("  target   : %dHz / %dch / %s wav%s", cfg.sample_rate, cfg.channels, cfg.codec,
             " (passthrough allowed)" if cfg.passthrough_wav else "")
    log.info("  ffmpeg   : %s", shutil.which(cfg.ffmpeg_bin))
    if cfg.mock:
        log.warning("MOCK mode: nothing is forwarded upstream")

    try:
        _serve(app, cfg, args.server)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        log.error("could not bind %s:%d -- %s", cfg.listen_host, cfg.listen_port, exc)
        return 1
    return 0


def _serve(app: "Flask", cfg: Config, choice: str) -> None:
    """Prefer waitress when available (no dev-server warning); fall back to Flask."""
    if choice in ("auto", "waitress"):
        try:
            from waitress import serve  # type: ignore
        except ImportError:
            if choice == "waitress":
                log.error("waitress not installed: pip install waitress (or use --server flask)")
                raise
            log.info("waitress not installed -- using Flask's threaded dev server")
        else:
            log.info("serving with waitress (%d threads)", cfg.threads)
            serve(app, host=cfg.listen_host, port=cfg.listen_port, threads=cfg.threads,
                  recv_bytes=1 << 20, send_bytes=1 << 20,
                  channel_timeout=max(cfg.upstream_timeout + 30, 120))
            return
    app.run(host=cfg.listen_host, port=cfg.listen_port, threaded=True, debug=False)


if __name__ == "__main__":
    sys.exit(main())
