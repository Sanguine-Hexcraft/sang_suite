# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A streaming tools suite: a FastAPI backend plus a Vue 3 frontend that serves both a control panel (`/control`) and browser-source overlay pages for OBS (`/overlay/alert` and `/overlay/media`). Overlay views need transparent backgrounds — note that `<style scoped>` cannot style `body`; use a global style or `:global(body)` for that.

The two overlays are separate pages deliberately: one page would mean a clip and a follow alert fighting over a single DOM and a single audio context.

## Running the app

### For development

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

### For streaming

Vite is a development tool only — on stream the backend serves the built frontend itself, so it's one process and no :5173:

```sh
./stream.sh              # npm run build, then `fastapi run` on :8000
./stream.sh --skip-build # skip the rebuild when the frontend hasn't changed
```

OBS browser sources therefore point at `http://localhost:8000/overlay/alert`, *not* the Vite port. The two modes can't run at once — both want :8000.

`stream.sh` rebuilds by default because a stale `frontend/dist` fails silently: the mount happily serves an old build, so the overlay looks like the code changes never landed.

## Architecture notes

- `backend/main.py` holds everything except the Twitch EventSub client (`backend/twitch.py`). All routes are prefixed `/api` (and websockets `/ws`) so the Vite proxy picks them up — don't add unprefixed routes.
- In production the backend also serves `frontend/dist/` at `/` via an SPA-aware `StaticFiles` mount. It is registered at the *bottom* of `main.py` on purpose — a mount at `/` shadows anything declared after it, so new routes must go above it. Note that Starlette's `StaticFiles` signals a miss by *raising* `HTTPException(404)` rather than returning a 404 response, which is why the fallback is a `try/except` and not a status check.
- Widget settings (alert duration, per-kind labels and accent colours) and channel point reward routing live in `backend/config.json`, gitignored, with defaults in the Pydantic models. `PUT /api/config` saves and then broadcasts the new config over `/ws`, so overlays update live; the frontend store routes `type: 'config'` messages to `config` and everything else to `lastEvent`.
- **Adding an alert kind needs two edits**, not one: `DEFAULT_KINDS` in `main.py` *and* `backend/config.json`. The file replaces `kinds` wholesale at load, so a key added only to the defaults never reaches a machine that already has a config file.
- Every broadcast except `config` and `panic` gets a monotonic `id` plus an `at` timestamp, and is kept in a 50-message deque. The last 20 are persisted to gitignored `backend/activity.json` so the dashboard feed survives refreshes and restarts. **The saved file carries the sequence counter too, and that matters**: if `_seq` reset to 1 on reboot, an overlay still holding a higher `last_id` would treat every new event as already-seen and get no replay until the count climbed back past its watermark. A client sends `{type:'hello', last_id}` on reconnect and gets only the gap; `hello` is handled in the `/ws` endpoint and must never be relayed, or overlays receive it as a mystery event. The client watermark is module scope in `overlay.ts`, deliberately not `localStorage` — it should survive the reconnect loop but not a page reload.
- Clips and gifs live in gitignored `backend/media/`, served by a `StaticFiles` mount at `/media` **registered above the SPA mount** (see the shadowing rule below). Vite proxies `/media` alongside `/api` and `/ws`.
- Reward routing is injected into `twitch.py` as a *callable*, not a dict — `PUT /api/config` rebinds `config` wholesale, so a captured dict goes stale on the first dashboard save.
- Two distinct websockets: `/ws` is a server the browser dials into (the alert relay); the `OBSController` in `main.py` is a client that dials OUT to OBS's own websocket (`ws://localhost:4455`) to control scenes/sources. Both live in `main.py`.
- OBS control reads `OBS_WS_URL` / `OBS_WS_PASSWORD` from `backend/.env` (loaded by a tiny dependency-free reader; `.env` is gitignored). Copy `backend/.env.example` to `backend/.env` and fill it in. The backend starts fine without OBS running — connection is lazy and only errors on a control button press.
- Backend dependencies are pinned in `backend/requirements.txt`. On a fresh pull, rebuild the venv with `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`. When you add a package, install it into the venv and regenerate the pin (keep the `fastapi[standard]` extra).
- Frontend is a standard Vue 3 + TypeScript + Pinia + vue-router scaffold; routes are declared in `frontend/src/router/index.ts`, views in `frontend/src/views/`.

## Gotchas

- The dev machine runs **Bazzite**, an atomic (image-based) Fedora derivative — `uname` reports `fc44`, but it is *not* ordinary Fedora. The base image is read-only, so **`dnf install` does not work**. Install CLI tooling with `brew` (Homebrew ships with Bazzite); reserve `rpm-ostree install` for things that genuinely must be layered into the image, since it needs a reboot. Don't suggest `dnf` here.
- Every `.vue` file with a script block must use `<script setup lang="ts">`. Plain `<script setup>` compiles fine but breaks `vue-tsc` with TS7016 ("could not find a declaration file") at the import site, because `allowJs` is off.
- **Encode overlay clips as WebM (VP9 + Opus).** OBS's bundled CEF on Linux frequently ships without proprietary codecs, so an H.264/AAC MP4 can play silently or not at all *while working perfectly in a normal browser*. `ffmpeg -i in.mp4 -c:v libvpx-vp9 -crf 32 -b:v 0 -row-mt 1 -c:a libopus -b:a 128k out.webm`. Also tick "Control audio via OBS" on the browser source.
- **Never put a fixed timeout on media playback.** A cap intended as a stall backstop truncates any clip longer than it — a 30s cap cut a 75s clip in half, and the audio kept going because `v-show` hides an element without pausing it. Watchdogs must measure *lack of progress* (rearm on `timeupdate`), and whatever ends a clip must pause it and clear its `src`.
- `channel.subscribe` fires for **new subscribers only**. Renewals are `channel.subscription.message`; gifter announcements are `channel.subscription.gift`. All three share the `channel:read:subscriptions` scope, so adding them needs no re-authorize.
- The Twitch token must be authorized as the **broadcaster** (`sanguine_______`), even though the app registration lives on the bot account. Authorizing as the bot leaves every subscription rejected and `GET /helix/eventsub/subscriptions` returning zero enabled. The bot gets its own separate token in Phase 12, for *sending* chat only.
- Debugging "alerts stopped working": check `https://id.twitch.tv/oauth2/validate` for the token identity and scopes, then `GET /helix/eventsub/subscriptions` — subscriptions only report `enabled` while the session websocket is alive, so that one call proves both auth and connectivity. An expired token is rarely the cause.
- On one specific machine (a Fedora box / its network), SSH connections to GitHub are silently dropped, so an SSH remote hangs forever rather than erroring. If pushing hangs there, switch the remote to HTTPS (`https://github.com/Sanguine-Hexcraft/sang_suite.git`) with `gh` as the credential helper. SSH works fine on other machines — this is not a universal rule.
