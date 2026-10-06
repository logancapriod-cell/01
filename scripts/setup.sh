#!/usr/bin/env bash
set -euo pipefail
cd /workspace/01
export UV_CACHE_DIR=/workspace/.cache/uv
command -v uv >/dev/null
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
if [ ! -x .venv/bin/python ]; then
  uv venv --python 3.12 .venv
fi
uv pip sync --python .venv/bin/python requirements.txt
.venv/bin/python -c 'import fastapi, uvicorn, yt_dlp, PIL; print("Dependencies ready")'
ffmpeg -hide_banner -encoders 2>/dev/null | rg -q 'libx264'
test -f "${VIRALLAB_FONT:-/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc}"
