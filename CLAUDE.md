# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A streaming tools suite: a FastAPI backend plus a Vue 3 frontend that serves both a control panel (`/control`) and browser-source overlay pages for OBS (e.g. `/overlay/alert`). Overlay views need transparent backgrounds — note that `<style scoped>` cannot style `body`; use a global style or `:global(body)` for that.

## Running the app

Both servers must run simultaneously; the browser only talks to Vite, which proxies `/api` and `/ws` to the backend on port 8000 (see `frontend/vite.config.ts`).

Backend (FastAPI on :8000):
```sh
cd backend && source venv/bin/activate && fastapi dev main.py
```

Frontend (Vite on :5173):
```sh
cd frontend && npm run dev
```

Other frontend commands (run from `frontend/`):
- `npm run type-check` — vue-tsc; run this to verify TS changes
- `npm run build` — type-check + production build

There are no tests or linter configured yet, in either half.

## Architecture notes

- `backend/main.py` is the entire backend. All routes are prefixed `/api` (and websockets `/ws`) so the Vite proxy picks them up — don't add unprefixed routes.
- Two distinct websockets: `/ws` is a server the browser dials into (the alert relay); the `OBSController` in `main.py` is a client that dials OUT to OBS's own websocket (`ws://localhost:4455`) to control scenes/sources. Both live in `main.py`.
- OBS control reads `OBS_WS_URL` / `OBS_WS_PASSWORD` from `backend/.env` (loaded by a tiny dependency-free reader; `.env` is gitignored). Copy `backend/.env.example` to `backend/.env` and fill it in. The backend starts fine without OBS running — connection is lazy and only errors on a control button press.
- Backend dependencies are pinned in `backend/requirements.txt`. On a fresh pull, rebuild the venv with `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`. When you add a package, install it into the venv and regenerate the pin (keep the `fastapi[standard]` extra).
- Frontend is a standard Vue 3 + TypeScript + Pinia + vue-router scaffold; routes are declared in `frontend/src/router/index.ts`, views in `frontend/src/views/`.

## Gotchas

- The dev machine runs **Bazzite**, an atomic (image-based) Fedora derivative — `uname` reports `fc44`, but it is *not* ordinary Fedora. The base image is read-only, so **`dnf install` does not work**. Install CLI tooling with `brew` (Homebrew ships with Bazzite); reserve `rpm-ostree install` for things that genuinely must be layered into the image, since it needs a reboot. Don't suggest `dnf` here.
- Every `.vue` file with a script block must use `<script setup lang="ts">`. Plain `<script setup>` compiles fine but breaks `vue-tsc` with TS7016 ("could not find a declaration file") at the import site, because `allowJs` is off.
- On one specific machine (a Fedora box / its network), SSH connections to GitHub are silently dropped, so an SSH remote hangs forever rather than erroring. If pushing hangs there, switch the remote to HTTPS (`https://github.com/Sanguine-Hexcraft/sang_suite.git`) with `gh` as the credential helper. SSH works fine on other machines — this is not a universal rule.
