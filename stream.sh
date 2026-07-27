#!/usr/bin/env bash
# Start the suite the way the stream uses it: ONE process on :8000, serving both
# the API/websocket and the built frontend. No Vite — that's only for coding,
# where its dev server proxies /api and /ws back to this same backend.
#
#   ./stream.sh              rebuild the frontend, then serve
#   ./stream.sh --skip-build serve the existing build (faster; use when the
#                            frontend hasn't changed since last time)
#
# OBS browser source:  http://localhost:8000/overlay/alert
# Control panel:       http://localhost:8000/control
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" != "--skip-build" ]; then
  # The backend mounts frontend/dist at "/", so a stale build silently serves
  # yesterday's overlay. Cheap insurance to rebuild every launch.
  (cd frontend && npm run build)
fi

cd backend
source venv/bin/activate
# `run`, not `dev`: no reloader, no file watcher. exec so Ctrl-C stops the
# server directly instead of leaving it orphaned behind this script.
exec fastapi run main.py
