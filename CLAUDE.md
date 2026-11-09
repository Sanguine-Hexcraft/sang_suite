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
- **The M&M death counter** (`backend/mnm.py`, `/overlay/deaths`) reads Monsters & Memories' *Ledger*, a per-character JSON event journal at `<settings root>/<server>/<Char>/Ledger/<Char>_Character_YYYY-MM-DD.json`. Event keys are opaque (`f01` action, `f04` timestamp, `f05` `zone_`+base64 name, `f07` character), and `f01 == "act_02"` is a death. The settings root is auto-discovered from the umu Wine prefixes; `MNM_SETTINGS_DIR` in `.env` overrides it. Three things to know:
  - The game rewrites the whole file during play rather than appending, so a parse failure just means the file was caught mid-write; it is retried on the next 2 s poll.
  - Some timestamps carry no offset. They are read as local time, because comparing naive and aware datetimes raises.
  - `deaths.json` (gitignored) stores only `session_start` and a manual `adjust`, never the count. The count is recomputed from Ledger timestamps, which is why a restart can't lose or double it. The first poll after boot marks existing deaths as already announced, so a restart doesn't re-alert them.
- **The M&M Ledger ingester** (`backend/ledger.py`) copies *every* Ledger event — Character, Social and `Ledger/Archive/` files — into gitignored `backend/ledger.db` (SQLite) on a 2 s poll, broadcasts your own level-ups as `level_up` alerts, and serves `GET /api/ledger/summary` (since the death counter's stream start by default) and `GET /api/ledger/recent`. Things to know:
  - **Your own loot, coin and level-ups are written to both the Character and the Social file**, a second or so apart. The Social copy is stored with `mirror = 1`; query the `ledger` view (which excludes mirrors), not `events`, or totals double.
  - Rows are keyed by a hash of the raw event and inserted with `INSERT OR IGNORE`, because files overlap (archives repeat day-file events) and are rewritten whole. Re-ingesting is always safe; deleting `ledger.db` rebuilds it from whatever the game still has on disk.
  - Any `dNN` string in `f03` may or may not be base64, even for the same field, so `_unb64` try-decodes each. `act_14` is coin looted from a corpse, not a kill.
  - `ANNOUNCE` picks which kinds broadcast live; `ALERTS` picks which of those go out as `type: "alert"` (each needs a `DEFAULT_KINDS` entry *and* a `config.json` one) rather than `type: "ledger"`, which only the activity feed shows. The first poll is a silent backfill.
- **The M&M zone banner** (`backend/zones.py`, `/overlay/zone`) tails `Player.log`, not the Ledger: it remembers `Start zoning process to <zone>` and broadcasts `type: "zone"` on `Done waiting, hide LoadingPanel`, so the banner shows as the loading screen clears. The first read of a log is silent (it only sets the current zone for `GET /api/zone`); a log that shrinks or changes inode — the game starts a new one each launch — is read from the top. Names and lore come from committed `backend/zones.json`, re-read per zone change. The overlay ignores `replay` messages, and its whole show/hold/fade is one CSS animation ended by `animationend`, for the same OBS-timer reason as the death plaque.
- **The bot** (`backend/chat.py`, Phase 12) is a second Twitch identity with its own `Twitch` instance and its own token cache, gitignored `backend/.chat_tokens.json`, authorized as `agent_of_innoruuk` with only `user:write:chat`. It only *sends*, via Helix (`POST /api/chat`); `twitch.py`'s broadcaster token only reads. On first run click "Not you?" on Twitch's consent page to switch to the bot account; if it got authorized as the wrong user, delete the file and restart (startup warns when the bot id equals the channel's). Its rate limiter (20 per 30 s) *drops* rather than queues, because a late greeting is worse than none, and counts attempts, not successes. `send(text, reply_to=message_id)` threads a reply under a chat message.
- **Song requests** (`backend/songs.py`, `/overlay/song`, Phase 13, done, plus text search). `SongQueue.request(text, user, user_id)` is the one entry point, used by both `POST /api/songs/request` and `!sr`. Things to know:
  - Only the 11-character video id is kept, never the URL. The overlay will build `youtube.com/embed/<id>` itself, so chat text can't point it anywhere else.
  - The queue lives on the backend (unlike the alert queue) and is saved to gitignored `backend/songs.json` as `now` + `queue`. `songs` broadcasts are state, so they're excluded from replay the way `config` and `deaths` are; clients fetch `GET /api/songs` instead.
  - Rules come from `config.songs` (`SongConfig`) through a lambda, for the same stale-capture reason as reward routing. `enabled` defaults to **off**.
  - **The overlay follows, the backend decides.** `SongOverlay.vue` plays whatever `songs.now` is and reports `POST /api/songs/ended {request_id, reason}` when a song ends, errors or stalls; the backend advances and broadcasts. A report naming a song that's no longer `now` is ignored, which is what stops two open copies of the overlay skipping two songs.
  - `paused` is queue state, saved to disk, not an overlay command: panic sets it, so a reloaded overlay comes back silent. Resume is `POST /api/songs/resume`.
  - The stall watchdog only skips a song that *was* playing and stopped advancing. One that never started isn't skipped, because a normal browser tab blocks autoplay with sound, and a forgotten tab would otherwise skip every song while OBS played them fine.
  - Every exit from `now` goes through `SongQueue.skip(outcome, detail)`, which writes the played history (`played` / `skipped` / `failed` / `removed`, newest first, last 50). It's saved in `songs.json` and rides on every `songs` broadcast, feeding the dashboard's Song Requests tab. Songs removed before they played aren't in it.
  - The Song Requests tab's card changes `enabled` and `volume` through `POST /api/songs/settings`, never a whole-config PUT. The settings tab's draft is cloned at page load, so its Save sends only `alerts` and takes the rest from the store; sending the draft whole would undo an open/close made since. Opening or closing requests is announced in chat by the bot.
  - Free checks run before the YouTube lookup (`YOUTUBE_API_KEY` in `.env`, 1 quota unit each), and the queue checks run again after it, because the lookup awaits. No key means requests are refused, never queued unchecked.
  - **Search** (`!sr <title and/or artist>`, when no link parses) costs 100 quota units against 1 for a link, so: every free check runs first, then a daily budget (`songs.search_daily_budget`, counted per *Pacific* day because that's when Google resets quota, saved in `songs.json`), then a per-viewer cooldown. A search is charged the moment it's made, whatever it finds. The top 5 results are checked in one `lookup()` call and the first that passes `_refusal()` is queued, because the top hit is often a label video that blocks embedding. The bot's reply includes the link for searches so the viewer can `!wrongsong` a bad pick. `search_enabled` turns it off while leaving links on.
- **Chat commands** (`backend/commands.py`, Phase 13c). `twitch.py` subscribes to `channel.chat.message` and `channel.chat.message_delete` with the broadcaster token (scope from Phase 10, no re-auth), turns each into a plain `ChatLine` and hands it to injected callbacks; it knows nothing about commands. `ChatCommands.handle` drops, before parsing anything: Shared Chat messages from other channels (`source_channel_id`), the bot itself (by user id), and logins in `config.chat.bot_denylist`. Permissions come from the message's badges. Commands: `!sr`, `!song`, `!queue`, `!wrongsong`, mod-only `!skip`, and `!lurk` (puts `media/gifs/lurky.gif` up via the media overlay, 10 min per-viewer cooldown). A mod deleting an `!sr` message removes that request. Things to know:
  - Replies go through an injected `reply` callable. Real chat threads them via `ChatBot.send`; a dropped reply (rate limit) is only logged, and the command has already taken effect by then.
  - `POST /api/chat/test {text, user, badges}` runs the real dispatcher and *returns* the replies instead of posting them, so commands are testable without a second account; side effects (queueing, the lurk gif) are real. `POST /api/chat/test/delete/{message_id}` fakes a mod delete.
  - Loot boxes (Phase 15) should hook into `ChatCommands.handle`, not subscribe to chat a second time.
- **Nothing has a login, so the backend is local-only.** `stream.sh` binds `127.0.0.1`. `TrustedHostMiddleware` refuses any Host but `localhost`/`127.0.0.1` (DNS rebinding), and a non-local `Origin` is refused on non-GET requests and on the `/ws` handshake (a page you visit could otherwise POST to the API or open the socket, since websockets ignore CORS). A missing Origin is allowed, because that's curl, not a page. `/ws` relays only the types in `RELAYABLE` — **a new client-sent test message type has to be added there or it's dropped (with only a `[ws] dropped` log line)** — and refuses any `media` whose `src` isn't under `/media/`, a check `MediaOverlay.vue` repeats.
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
- The Twitch token must be authorized as the **broadcaster** (`sanguine_______`), even though the app registration lives on the bot account. Authorizing as the bot leaves every subscription rejected and `GET /helix/eventsub/subscriptions` returning zero enabled. The bot has its own separate token (`chat.py`, `.chat_tokens.json`), for *sending* chat only.
- Debugging "alerts stopped working": check `https://id.twitch.tv/oauth2/validate` for the token identity and scopes, then `GET /helix/eventsub/subscriptions` — subscriptions only report `enabled` while the session websocket is alive, so that one call proves both auth and connectivity. An expired token is rarely the cause.
- On one specific machine (a Fedora box / its network), SSH connections to GitHub are silently dropped, so an SSH remote hangs forever rather than erroring. If pushing hangs there, switch the remote to HTTPS (`https://github.com/Sanguine-Hexcraft/sang_suite.git`) with `gh` as the credential helper. SSH works fine on other machines — this is not a universal rule.
