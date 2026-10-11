# openai-stt-ffmpeg-proxy

A tiny OpenAI-compatible speech-to-text proxy that runs every upload through
**ffmpeg** before it reaches the transcription backend.

Clients POST to `/v1/audio/transcriptions` exactly as they would to OpenAI. The
proxy decodes whatever they sent — browser `MediaRecorder` webm/opus, m4a/mp4,
mp3, ogg, flac, amr, video containers, anything ffmpeg can read — into
**16 kHz mono 16-bit PCM WAV**, forwards that WAV plus all the other form fields
upstream, and relays the answer back verbatim.

```
client ──multipart POST──▶ openai-stt-ffmpeg-proxy ──ffmpeg──▶ 16 kHz mono WAV
  webm/opus, m4a, mp3 …             (this project)

openai-stt-ffmpeg-proxy ──multipart POST (wav + fields)──▶ /v1/audio/transcriptions
                                   OpenAI / Azure / local whisper.cpp / …
```

Why: most STT backends are fussy about containers and codecs, and 16 kHz mono
PCM is the one shape every transcription model accepts. Normalizing at the edge
turns "400 Invalid audio format" into a non-issue, and lets a browser record and
transcribe without any client-side encoding.

## Requirements

- Python 3.10+ with `flask` and `requests` (`pip install -r requirements.txt`)
- `ffmpeg` on `PATH` (ffprobe is used for logging when present, and optional)

## Quick start

```bash
export UPSTREAM_API_KEY=***           # or pass --api-key, or let clients send their own
python3 stt_proxy.py --upstream https://api.openai.com
```

```bash
# no key, no network: verify ffmpeg plumbing end to end
python3 stt_proxy.py --selftest
python3 stt_proxy.py --mock --port 8765      # echo conversion details instead of forwarding
python3 smoke_test.py                        # 27 end-to-end checks against a fake upstream
```

Point a client at it — same API, same fields:

```bash
curl -s http://127.0.0.1:8765/v1/audio/transcriptions \
  -F file=@voice-note.webm \
  -F model=whisper-1 \
  -F language=de \
  -H "Authorization: Bearer $UPSTREAM_API_KEY"
```

Or use the bundled client, which can also generate a test clip:

```bash
python3 test_client.py --make-sample --url http://127.0.0.1:8765/v1/audio/transcriptions
```

## Docker deployment (distroless)

Runs on **`gcr.io/distroless/python3-debian13:nonroot`** — Python 3.13, uid 65532,
no shell, no apt, no pip, no package manager… **and no ffmpeg**. The [Dockerfile](Dockerfile)
therefore builds the runtime in three stages and copies only artifacts into the
distroless rootfs:

| Stage | Base | Produces |
| --- | --- | --- |
| `deps` | `python:3.13-slim-trixie` | `pip install --target` → `/app/deps` (flask, requests, waitress) |
| `ffmpeg` | `debian:trixie-slim` | `apt install ffmpeg` → `/usr/local/bin/{ffmpeg,ffprobe}` + their `ldd` closure in `/opt/ffmpeg/lib` |
| `runtime` | `gcr.io/distroless/python3-debian13:nonroot` | the two above, plus `stt_proxy.py` and `healthcheck.py` |

Debian publishes no static ffmpeg build, so the ffmpeg stage walks
`ldd ffmpeg ffprobe` and copies every shared object they actually resolve
(204 of them), minus glibc / libgcc_s / libstdc++ / zlib, which the distroless
base already carries. Builder and runtime are the same Debian release, so those
shared libraries are version-identical and nothing is shadowed.

```bash
# podman needs --format docker, otherwise HEALTHCHECK is silently dropped
podman build --format docker -t openai-stt-ffmpeg-proxy:distroless .
docker build  -t openai-stt-ffmpeg-proxy:distroless .          # docker keeps HEALTHCHECK by default

docker run -d --name openai-stt-ffmpeg-proxy -p 8765:8765 \
  -e STT_PROXY_API_KEY="$UPSTREAM_API_KEY" \
  --read-only --tmpfs /tmp:size=1024M,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges=true \
  openai-stt-ffmpeg-proxy:distroless
```

Or with compose — [compose.yml](compose.yml) carries the same hardening and maps
every knob to a `.env` variable:

```bash
cp .env.example .env      # set STT_PROXY_API_KEY
docker compose up -d --build
curl -s localhost:8765/health
```

`compose.yml` is Compose v2's default filename, so no `-f` flag is needed
(`podman-compose` picks it up the same way).

Why those flags:

- **`--read-only` + `--tmpfs /tmp`** — the proxy streams uploads to `$TMPDIR` and
  deletes them when the response completes; nothing else in the image is written.
- **`--cap-drop ALL`, `no-new-privileges`, uid 65532** — the process needs no
  capabilities, and 8765 is above the privileged-port boundary.
- **`HEALTHCHECK`** runs [healthcheck.py](healthcheck.py) rather than curl, because
  distroless has no shell; it fails the container if `/health` is down *or* if
  ffmpeg has somehow disappeared from the image.

To move Debian releases, keep the Python minor version matched in both builder
and runtime:

```bash
podman build --format docker \
  --build-arg DISTROLESS_BASE=gcr.io/distroless/python3-debian12:nonroot \
  --build-arg PYTHON_BUILDER=python:3.11-slim-bookworm \
  --build-arg DEBIAN_BUILDER=debian:bookworm-slim \
  -t openai-stt-ffmpeg-proxy:distroless .
```

Verify a build with [container_test.sh](container_test.sh) — it drives the HTTP
surface from outside (distroless has no shell to exec into), converts a real
webm/opus clip through the running container, runs the image's own healthcheck,
and checks uid / read-only rootfs / capabilities:

```bash
./container_test.sh                 # build + test
./container_test.sh --no-build      # test the existing image
CONTAINER_CLI=docker ./container_test.sh
```

Image size is ~294 MB: ~63 MB distroless base, ~222 MB ffmpeg closure (Debian's
ffmpeg links every codec under the sun — x265, SVT-AV1, flite, libplacebo…),
~8 MB python deps. The transcription path only needs a fraction of that; see
[Notes](#notes) for trimming it.

## Configuration

Every flag has an `STT_PROXY_*` environment equivalent.

| Flag | Env | Default | Meaning |
| --- | --- | --- | --- |
| `--host` / `--port` | `STT_PROXY_HOST` / `STT_PROXY_PORT` | `127.0.0.1` / `8765` | bind address |
| `--upstream` | `STT_PROXY_UPSTREAM` | `https://api.openai.com` | upstream base URL |
| `--path` | `STT_PROXY_PATH` | `/v1/audio/transcriptions` | upstream path |
| `--mount` | `STT_PROXY_MOUNT` | `/v1/audio/transcriptions` | path this proxy serves |
| `--api-key` | `STT_PROXY_API_KEY` | – | bearer token for upstream; without it the client's `Authorization` is passed through |
| `--sample-rate` | `STT_PROXY_SAMPLE_RATE` | `16000` | target sample rate |
| `--channels` | `STT_PROXY_CHANNELS` | `1` | target channels (1 = mono) |
| `--codec` | `STT_PROXY_CODEC` | `pcm_s16le` | WAV PCM codec (`pcm_f32le` also works) |
| `--input-format` | `STT_PROXY_INPUT_FORMAT` | auto-detect | force ffmpeg's input demuxer (`webm`, `ogg`, …) for truncated/odd streams |
| `--ffmpeg` / `--ffprobe` | `STT_PROXY_FFMPEG` / `STT_PROXY_FFPROBE` | `ffmpeg` / `ffprobe` | executables |
| `--ffmpeg-timeout` | `STT_PROXY_FFMPEG_TIMEOUT` | `180` | seconds |
| `--upstream-timeout` | `STT_PROXY_UPSTREAM_TIMEOUT` | `300` | seconds |
| `--max-upload-mb` | `STT_PROXY_MAX_UPLOAD_MB` | `200` | hard upload cap |
| `--passthrough-wav` | `STT_PROXY_PASSTHROUGH_WAV=1` | off | skip ffmpeg when the input already matches the target shape |
| `--save-dir` | `STT_PROXY_SAVE_DIR` | – | also keep each converted WAV (debugging) |
| `--forward-header` | `STT_PROXY_FORWARD_HEADERS` | auth headers | extra client headers to forward, repeatable |
| `--server` | `STT_PROXY_SERVER` | `auto` | `waitress` if installed, else Flask dev server |
| `--threads` | `STT_PROXY_THREADS` | `8` | waitress worker threads |
| `--mock` | `STT_PROXY_MOCK=1` | off | convert, then reply with details instead of calling upstream |
| `--keep-tmp` | `STT_PROXY_KEEP_TMP=1` | off | leave temp files behind |

The ffmpeg invocation, per file:

```
ffmpeg -hide_banner -loglevel error -nostdin -y -probesize 10M \
       -i <upload> -map 0:a:0 -ac 1 -ar 16000 -c:a pcm_s16le -f wav <out>
```

## Behaviour

- **Fields**: every form field except `file` is forwarded untouched (`model`,
  `language`, `prompt`, `temperature`, `response_format`, …). The file part is
  re-emitted as `<original-name>.wav` with `Content-Type: audio/wav`, since some
  backends sniff the extension.
- **Auth**: the client's `Authorization` / `x-api-key` header is passed through by
  default; `--api-key` overrides it so clients never see the credential.
- **Responses**: status, body and non-hop-by-hop headers are relayed as-is, so
  `response_format=text|json|verbose_json|srt|vtt` all work, and upstream errors
  arrive unchanged. `X-Stt-Ffmpeg-Proxy: ffmpeg` and `X-Stt-Ffmpeg-Proxy-Converted: 0|1` are
  added.
- **Errors**: `400` missing/empty file, `413` over the size cap, `415` not
  multipart, `422` ffmpeg could not decode it (stderr tail included), `502`/`504`
  upstream unreachable/slow, `500` ffmpeg missing.
- **Temp files**: uploads are streamed to a temp dir (never buffered whole in
  RAM) and deleted when the response completes.

## Production notes

- Install `waitress` and the proxy uses it automatically (`--server auto`);
  otherwise it runs Flask's threaded dev server, which is fine on localhost but
  logs a warning.
- Bind to `127.0.0.1` and put nginx/caddy in front for TLS and to raise the body
  size limit (`client_max_body_size 200m;`).
- Long recordings: raise `--upstream-timeout` and `--threads`; conversion of a
  60-minute mono track takes well under a second.
- Non-OpenAI backends: `--upstream https://your-host --path /some/stt/route`, or
  pass a full URL as `--path`.

## Notes

**Accepted input formats.** Nothing is format-gated: the proxy never looks at the
file extension or the MIME type of the multipart part, it hands the bytes to
ffmpeg, which probes the container itself. Anything ffmpeg can decode is
accepted. Verified end to end through the proxy:

| input | what ffmpeg found | forwarded as |
| --- | --- | --- |
| `voice.ogg` | vorbis, 48 kHz mono | 16 kHz mono s16le |
| `opus.ogg` | opus, 48 kHz stereo | 16 kHz mono s16le |
| `song.mp3` | mp3, 44.1 kHz | 16 kHz mono s16le |
| `opus.webm` (what `MediaRecorder` emits) | opus, 48 kHz | 16 kHz mono s16le |
| `clip.m4a` | aac, 44.1 kHz | 16 kHz mono s16le |
| `lossless.flac` | flac, 48 kHz | 16 kHz mono s16le |
| `video.mp4` with no audio track | — | rejected, `422` |

Undecodable uploads come back as `422` with the last lines of ffmpeg's stderr,
which is usually the quickest way to see what a client actually sent.

**Image size.** ~294 MB, of which ~222 MB is Debian's ffmpeg shared-library
closure (204 libraries — x265, SVT-AV1, codec2, flite TTS, libplacebo, lapack —
none of which a transcription proxy touches). They cannot simply be dropped from
the copy: they are `NEEDED` entries, so ffmpeg would not load without them, and
Debian publishes no static ffmpeg build. For a genuinely small image, replace the
`apt install ffmpeg` stage with a minimal static build:

```
./configure --disable-shared --enable-static --extra-ldflags=-static \
  --disable-doc --disable-everything --disable-network \
  --enable-ffmpeg --enable-ffprobe \
  --enable-demuxer=matroska,webm,ogg,mp3,mov,mp4,aac,flac,wav,adts \
  --enable-decoder=opus,vorbis,mp3,aac,flac,pcm_s16le,pcm_f32le \
  --enable-encoder=pcm_s16le --enable-muxer=wav \
  --enable-filter=aresample,aformat
```

That is roughly 25 MB of binaries instead of 222 MB of codec libraries (~90 MB
image), at the cost of a source build in CI and losing exotic inputs (amr, wma,
…). The `apt` variant stays the default because it accepts anything.

## Files

| File | Purpose |
| --- | --- |
| [`stt_proxy.py`](stt_proxy.py) | the proxy — CLI, ffmpeg conversion, forwarding |
| [`healthcheck.py`](healthcheck.py) | stdlib-only `/health` probe, used by the image `HEALTHCHECK` |
| [`Dockerfile`](Dockerfile) | 3-stage distroless build (deps / ffmpeg / runtime) |
| [`compose.yml`](compose.yml) | hardened compose service definition, `.env`-driven |
| [`.env.example`](.env.example) | documented template for every compose variable |
| [`.dockerignore`](.dockerignore) | keeps the build context to the three files the image needs |
| [`smoke_test.py`](smoke_test.py) | end-to-end checks against a fake upstream (no key needed) |
| [`container_test.sh`](container_test.sh) | runs and verifies the built image (podman or docker) |
| [`test_client.py`](test_client.py) | CLI client; can generate a test webm clip |
| [`requirements.txt`](requirements.txt) | `flask`, `requests`, `waitress` |
