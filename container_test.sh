#!/usr/bin/env bash
#
# container_test.sh -- verify the built openai-stt-ffmpeg-proxy image actually works.
#
# Distroless has no shell, so nothing here exec()s into the container: it drives
# the HTTP surface from outside and asks the app to report on its own environment.
#
#   ./container_test.sh                 # build, then test
#   ./container_test.sh --no-build      # test the existing image
#   CONTAINER_CLI=docker ./container_test.sh
#
set -euo pipefail

IMAGE=${IMAGE:-openai-stt-ffmpeg-proxy:distroless}
PORT=${PORT:-18899}
NAME=openai-stt-ffmpeg-proxy-test
CLI=${CONTAINER_CLI:-}
MOCK=1
NO_BUILD=0

for arg in "$@"; do
  case $arg in
    --no-build) NO_BUILD=1 ;;
    --real)     MOCK=0 ;;   # needs UPSTREAM_API_KEY; hits the real upstream
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [ -z "$CLI" ]; then
  if command -v podman >/dev/null 2>&1; then CLI=podman
  elif command -v docker >/dev/null 2>&1; then CLI=docker
  else echo "need podman or docker" >&2; exit 2; fi
fi

fails=0
ok()  { printf '  PASS %s %s\n' "$1" "${2-}"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2-}"; fails=$((fails + 1)); }
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1" "($3)"; else bad "$1" "expected '$2' got '$3'"; fi
}

cleanup() { $CLI rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ---------------------------------------------------------------- build ----
if [ "$NO_BUILD" = 0 ]; then
  echo "build:"
  # --format docker keeps the HEALTHCHECK instruction: podman's default OCI
  # format drops it ("HEALTHCHECK is not supported for OCI image format").
  build_args=(-t "$IMAGE")
  if [ "$CLI" = podman ]; then build_args+=(--format docker); fi
  if $CLI build "${build_args[@]}" . >build.log 2>&1; then
    ok "image built" "$($CLI images --format '{{.Size}}' "$IMAGE" | head -1)"
  else
    bad "image build failed"; tail -25 build.log; exit 1
  fi
fi

# ------------------------------------------------------------- preflight ---
echo "image contents:"
$CLI run --rm -i --entrypoint /usr/bin/python3 "$IMAGE" - <<'PY'
import json, os, sys
print(json.dumps({
    "python": sys.version.split()[0],
    "uid": os.getuid(),
    "ffmpeg": os.path.exists("/usr/local/bin/ffmpeg"),
    "ffprobe": os.path.exists("/usr/local/bin/ffprobe"),
    "shell_present": os.path.exists("/bin/sh") or os.path.exists("/bin/bash"),
    "app": os.path.exists("/app/stt_proxy.py"),
    "deps": sorted(p for p in os.listdir("/app/deps") if not p.endswith(".dist-info"))[:6],
}))
PY
if [ $? -ne 0 ]; then bad "could not inspect image"; fi

# --------------------------------------------------------------- run ------
echo "run (hardened):"
ENV_ARGS=()
if [ "$MOCK" = 1 ]; then ENV_ARGS+=(-e STT_PROXY_MOCK=1); fi
$CLI run -d --name "$NAME" \
  -p "$PORT:8765" \
  --read-only \
  --tmpfs /tmp:size=256m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges=true \
  --pids-limit 256 \
  "${ENV_ARGS[@]}" \
  "$IMAGE" >/dev/null

echo "  container: $($CLI ps --filter name="$NAME" --format '{{.Status}}' | head -1)"

# --------------------------------------------------------------- health ---
echo "health:"
healthy=""
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 0.5
done
if [ -z "$healthy" ]; then
  bad "proxy never became healthy"; $CLI logs "$NAME" | tail -20; exit 1
fi
health=$(curl -fsS "http://127.0.0.1:$PORT/health")
echo "  $health"
check "health status ok" "ok" "$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
check "ffmpeg found in container" "True" \
  "$(printf '%s' "$health" | python3 -c 'import json,sys; print(bool(json.load(sys.stdin)["ffmpeg"]))')"

if $CLI healthcheck run "$NAME" >/dev/null 2>&1; then
  ok "image HEALTHCHECK passes"
else
  bad "image HEALTHCHECK failed"
fi

# ------------------------------------------------------------- convert ----
echo "conversion through the container:"
tmp=$(mktemp -d)
ffmpeg -hide_banner -loglevel error -y -f lavfi \
  -i "sine=frequency=440:sample_rate=48000" -t 2 -ac 2 -c:a libopus "$tmp/voice.webm"

resp=$(curl -fsS -X POST "http://127.0.0.1:$PORT/v1/audio/transcriptions" \
        -F "file=@$tmp/voice.webm;type=audio/webm" \
        -F "model=whisper-1" \
        -F "language=de")

if [ "$MOCK" = 1 ]; then
  echo "  $resp" | head -3
  check "converted sample rate" "16000" \
    "$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stt_proxy_mock"]["forwarded_wav"]["sample_rate"])')"
  check "converted channels" "1" \
    "$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stt_proxy_mock"]["forwarded_wav"]["channels"])')"
  check "converted bits" "16" \
    "$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stt_proxy_mock"]["forwarded_wav"]["bits_per_sample"])')"
  check "duration preserved (~2s)" "2.002" \
    "$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stt_proxy_mock"]["forwarded_wav"]["duration_s"])')"
  check "input probe saw opus" "opus" \
    "$(printf '%s' "$resp" | python3 -c 'import json,sys; print(json.load(sys.stdin)["stt_proxy_mock"]["received_probe"]["codec"])')"
else
  echo "  upstream reply: $(printf '%s' "$resp" | head -c 200)"
fi

# ------------------------------------------------------------ hardening ---
echo "runtime hardening:"
check "runs as uid 65532" "65532" "$($CLI inspect -f '{{.ProcessConfig.User}}' "$NAME" 2>/dev/null || $CLI inspect -f '{{.Config.User}}' "$NAME")"
check "rootfs read-only" "true" "$($CLI inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$NAME")"
# Effective capabilities, read from inside the running container (no shell
# needed -- distroless still has python).
caps=$($CLI exec "$NAME" /usr/bin/python3 -c \
  "print(open('/proc/self/status').read().split('CapEff:')[1].split()[0])" 2>/dev/null || echo "unknown")
if [ "$caps" = "0000000000000000" ] || [ "$caps" = "0" ]; then
  ok "process has no effective capabilities" "CapEff=$caps"
else
  bad "unexpected capabilities" "CapEff=$caps"
fi
if $CLI top "$NAME" -o pid,user,args 2>/dev/null | tail -n +2 | grep -qv "65532\|root"; then
  bad "a process is not running as nonroot"
else
  ok "process runs as nonroot"
fi

# ------------------------------------------------------------- errors -----
echo "error handling in the container:"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/v1/audio/transcriptions" \
        -F "file=@/etc/hostname;type=text/plain" -F "model=whisper-1")
check "undecodable upload -> 422" "422" "$code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/v1/audio/transcriptions" \
        -F "model=whisper-1")
check "missing file -> 400" "400" "$code"

$CLI logs "$NAME" 2>&1 | grep -E "listening|target|ffmpeg" | sed 's/^/  log: /'

echo
if [ "$fails" = 0 ]; then echo "container test: ALL PASS"; else echo "container test: $fails failure(s)"; fi
exit "$fails"
