#!/usr/bin/env bash
set -euo pipefail
cd /workspace/01
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
