# TubeSnip — multi-stage image (Alpine: small, and musl matches deno + ffmpeg).
# - builder: resolve deps with uv on Alpine (musl wheels; incl. nightly yt-dlp
#   via [tool.uv.sources]).
# - deno: COPYed from the official alpine image (single binary) — no curl install.
# - ffmpeg/ffprobe: `apk add --no-cache ffmpeg` — the Alpine-native package, no
#   cache layer. (Copy-from-image isn't clean for ffmpeg: no official image
#   ships a self-contained binary; linuxserver's libs are scattered.)

FROM ghcr.io/astral-sh/uv:python3.12-alpine AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY . .
RUN uv sync --frozen --no-dev

# yt-dlp-ejs JS runtime (yt-dlp's default) — official alpine deno image. The
# deno binary there is the glibc build, so it ships its own glibc loader + lib
# tree; copy all three (that's exactly what the deno:alpine image itself uses).
FROM denoland/deno:alpine AS deno

FROM python:3.12-alpine AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PATH="/usr/local/bin:/app/.venv/bin:${PATH}" \
    TUBESNIP_HOST=0.0.0.0
# ffmpeg + ffprobe (alpine package; --no-cache keeps the layer lean)
RUN apk add --no-cache ffmpeg
COPY --from=deno /bin/deno /usr/local/bin/deno
COPY --from=deno /lib/ld-linux-* /lib/
COPY --from=deno /usr/local/lib/glibc /usr/local/lib/glibc
COPY --from=builder /app /app
VOLUME ["/app/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys,urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3); sys.exit(0)" || exit 1
CMD [".venv/bin/tubesnip"]
