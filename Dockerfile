# syntax=docker/dockerfile:1
#
# openai-stt-ffmpeg-proxy on Google's distroless Python image.
#
#   gcr.io/distroless/python3-debian13:nonroot  ->  Python 3.13, uid 65532, no shell,
#                                                  no apt, no pip, and no ffmpeg.
#
# So this build assembles three things in throwaway builders and copies only the
# artifacts into the distroless rootfs:
#
#   deps    pip install --target      -> /app/deps        (flask, requests, waitress)
#   ffmpeg  apt install ffmpeg        -> /opt/ffmpeg      (binary + its ldd closure)
#   runtime distroless python3        -> the final image
#
# Debian ships no static ffmpeg, so the ffmpeg stage copies every shared library
# ffmpeg/ffprobe actually resolve, minus the handful distroless already carries
# (glibc, libgcc_s, libstdc++, zlib). Builder and runtime are the same Debian
# release, so those shared libs are version-identical.
#
# Supported base pairs (keep the Python minor version matched):
#   --build-arg DISTROLESS_BASE=gcr.io/distroless/python3-debian13:nonroot \
#   --build-arg PYTHON_BUILDER=python:3.13-slim-trixie  --build-arg DEBIAN_BUILDER=debian:trixie-slim
#   --build-arg DISTROLESS_BASE=gcr.io/distroless/python3-debian12:nonroot \
#   --build-arg PYTHON_BUILDER=python:3.11-slim-bookworm --build-arg DEBIAN_BUILDER=debian:bookworm-slim

ARG DISTROLESS_BASE=gcr.io/distroless/python3-debian13:nonroot
ARG PYTHON_BUILDER=python:3.13-slim-trixie
ARG DEBIAN_BUILDER=debian:trixie-slim

# --------------------------------------------------------------------------
# stage 1: python dependencies
# --------------------------------------------------------------------------
FROM ${PYTHON_BUILDER} AS deps

# --target keeps the layout flat and independent of the python minor version,
# so the same COPY works for the debian12 (3.11) and debian13 (3.13) bases.
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --target=/deps -r /build/requirements.txt

# --------------------------------------------------------------------------
# stage 2: ffmpeg + ffprobe and their shared library closure
# --------------------------------------------------------------------------
FROM ${DEBIAN_BUILDER} AS ffmpeg

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Already present in the distroless base (libc6 / libgcc-s1 / libstdc++6 / zlib1g):
# copying them again would only bloat the image and risk shadowing the base's copies.
RUN set -eux; \
    mkdir -p /out/bin /out/lib; \
    cp /usr/bin/ffmpeg /usr/bin/ffprobe /out/bin/; \
    ldd /usr/bin/ffmpeg /usr/bin/ffprobe \
        | grep -oE '/[^ ]+\.so[.0-9]*' \
        | sort -u > /closure.txt; \
    grep -vE '/(ld-linux|libc\.so|libm\.so|libdl\.so|libpthread\.so|librt\.so|libnss_|libresolv\.so|libgcc_s\.so|libstdc\+\+\.so|libz\.so)' \
        /closure.txt > /copy.txt || true; \
    while read -r lib; do \
        cp -L "$lib" "/out/lib/$(basename "$lib")"; \
    done < /copy.txt; \
    echo "copied $(wc -l < /copy.txt) shared libraries, $(du -sh /out | cut -f1)"; \
    LD_LIBRARY_PATH=/out/lib /out/bin/ffmpeg -hide_banner -version | head -1; \
    LD_LIBRARY_PATH=/out/lib /out/bin/ffprobe -hide_banner -version | head -1

# --------------------------------------------------------------------------
# stage 3: distroless runtime
# --------------------------------------------------------------------------
FROM ${DISTROLESS_BASE} AS runtime

# ffmpeg/ffprobe + the libs they need (flat dir, so this is arch-independent)
COPY --from=ffmpeg /out/bin/ffmpeg        /usr/local/bin/ffmpeg
COPY --from=ffmpeg /out/bin/ffprobe       /usr/local/bin/ffprobe
COPY --from=ffmpeg /out/lib/             /opt/ffmpeg/lib/

# python dependencies, then the app itself
COPY --from=deps /deps/                  /app/deps/
COPY stt_proxy.py                        /app/stt_proxy.py
COPY healthcheck.py                      /app/healthcheck.py

ENV PYTHONPATH=/app/deps \
    LD_LIBRARY_PATH=/opt/ffmpeg/lib \
    STT_PROXY_HOST=0.0.0.0 \
    STT_PROXY_PORT=8765 \
    STT_PROXY_SERVER=auto \
    TMPDIR=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# nonroot (uid 65532) -- the :nonroot base already runs unprivileged; this makes
# it explicit and survives a switch to the root-based tag.
USER nonroot
WORKDIR /app

EXPOSE 8765

# No shell and no curl in distroless, so the probe is a python script.
HEALTHCHECK --interval=30s --timeout=6s --start-period=15s --retries=3 \
    CMD ["/usr/bin/python3", "/app/healthcheck.py"]

ENTRYPOINT ["/usr/bin/python3", "/app/stt_proxy.py"]
