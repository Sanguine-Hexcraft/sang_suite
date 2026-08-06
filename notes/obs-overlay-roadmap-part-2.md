# Stream Overlay Manager — Build Roadmap, Part 2

Part 1 (`obs-overlay-roadmap.md`) took this from nothing to a working product: Phases 0–8, ending at one Python process serving both the dashboard and the overlays, with Twitch events driving alerts automatically.

Part 2 is about turning it from *working* into *something you'd trust on a live stream*. The tone changes accordingly — Part 1 explained what a Pinia store is, and you don't need that anymore. What follows is design decisions, the gotchas that will actually cost you an evening, and checkpoints.

## Where Part 1 left off

- One process. `./stream.sh` builds the frontend and runs `fastapi run` on :8000. OBS points at `http://localhost:8000/overlay/alert`.
- `/ws` relays events to every connected overlay. Twitch follows/subs/cheers/raids and manual alerts from `/control` are indistinguishable by the time they reach the overlay — that was the point.
- Widget settings live in `backend/config.json`, editable from `/control`, pushed live over the same relay.
- Alerts queue and display one at a time (commit `d5e56c3`), each for its full duration plus a 450ms gap.
- OBS scene/source control works, and stale browser sources get refreshed on startup.

## The shape of Part 2

```
Phase  9   Event delivery you can trust      ← infrastructure everything else leans on
Phase 10   Channel point redeems             ← new event source
Phase 11   The media overlay                 ← new output surface
Phase 12   The bot speaks                    ← new identity
Phase 13   Loot boxes                        ← composition of all of the above
```

The order is deliberate. Each phase is worth more once the one before it exists, and Phase 13 is last precisely *because* it's the least foundational — it's the payoff, not the plumbing.

**One cross-cutting constraint, read this before Phase 10:** every change to the OAuth scope list invalidates the cached token in `backend/.twitch_tokens.json` and forces a browser re-authorize. Phase 10 needs `CHANNEL_READ_REDEMPTIONS` and Phase 13 needs `USER_READ_CHAT`. **Add both at once in Phase 10**, even though you won't use the second for weeks. One re-auth instead of two.

---

## Phase 9 — Event delivery you can trust (1 evening)

`manager.broadcast` only reaches sockets that are connected at that instant. The frontend store reconnects after two seconds, but anything broadcast inside that window is gone forever. This has been a known deferred issue since Phase 8, and it's been survivable because the worst case was a missed follow alert.

It stops being survivable in Phase 10. A viewer who spends 5000 channel points and sees nothing happen has lost something real.

### The design

Give every broadcast a monotonically increasing id, keep the last 50 in a `collections.deque`, and let a reconnecting client ask for what it missed:

```python
# main.py, in ConnectionManager
self._seq = 0
self._history: deque[dict] = deque(maxlen=50)

async def broadcast(self, message: dict):
    self._seq += 1
    message = {**message, "id": self._seq}
    self._history.append(message)
    for ws in self.connections:
        await ws.send_text(json.dumps(message))
```

On connect the client announces the last id it saw, and the server replays the gap before normal traffic resumes:

```js
socket.onopen = () => {
  connected.value = true
  socket.send(JSON.stringify({ type: 'hello', last_id: lastSeenId }))
}
```

### The two details that make or break it

**`lastSeenId` is a module-level variable, not `localStorage`.** This is the whole subtlety of the phase. The case worth rescuing is the *reconnect loop* — `onclose` → `setTimeout(connect, 2000)` inside a page that never unloaded, which is what a backend restart or a network blip looks like. Module state survives that, so the replay fires. A full page reload is a different event: state is gone, `last_id` is null, and nothing replays. That's correct behavior — an OBS browser source starting fresh mid-stream should *not* dump an hour of backlog onto your canvas.

**`type: 'hello'` must not be relayed.** The `/ws` handler currently echoes everything it receives to everyone (`main.py`, the `while True` loop). A hello would be rebroadcast as a mystery event. Handle it in the endpoint and `continue`.

Config pushes don't need any of this — they're idempotent state, and the overlay fetches `/api/config` on mount anyway. Leave them out of the history buffer.

**Checkpoint:** with the overlay open, run `socket.close()` from devtools (or restart the backend), fire two alerts during the reconnect window, and watch both appear once it reconnects. Then hard-refresh the page and confirm you get *nothing* replayed.

---

## Phase 10 — Channel point redeems (1–2 evenings)

Mechanically the cheapest phase here: one more `listen_*` call in `twitch.py`, same shape as the four from Phase 7. The work is in the routing and the setup decisions.

Scope is `CHANNEL_READ_REDEMPTIONS` on the broadcaster token — plus `USER_READ_CHAT` at the same time, per the note above. Delete `.twitch_tokens.json`, re-authorize once in the browser, done.

### The gotcha that shapes your reward setup

**Your app can only manage redemptions for rewards it created itself.** Marking a redemption fulfilled or refunded requires `channel:manage:redemptions` *and* that the reward was created through the API with this same client ID. Rewards you make by hand in the Twitch dashboard are readable forever and manageable never.

So decide up front:

- **Read-only** — rewards live in the dashboard, the app just reacts. Simplest, and the right default. The cost is that a failed clip means the viewer's points are gone with no refund path.
- **App-managed** — the app creates rewards at startup from config and can auto-refund on failure. Costs you re-creating every reward once; the hand-made ones must be deleted or you'll have duplicates in the UI.

Start read-only. Migrating later is possible but viewers watch the rewards vanish and reappear, so do it between streams if you do it at all.

### Routing

Route on **reward id, not title** — titles get renamed on a whim and the id is stable. Which means you need a way to discover ids, so make an unconfigured redeem log itself in a copy-pasteable form:

```
[redeem] unconfigured reward 'Play a clip' id=9c8b7a6d-… cost=5000 — add it to config.json
```

That one print will save you a trip to the API explorer every time you add a reward.

Config grows a `rewards` section mapping id → what to do (`alert`, `media`, `chat`, or several). For this phase everything routes to the existing alert overlay, which proves the whole pipe with zero new UI.

**Checkpoint:** redeeming on your own channel fires an alert in OBS, with the reward title and the redeeming user's name, and no manual clicking.

---

## Phase 11 — The media overlay (2–3 evenings)

The first genuinely new output surface since Phase 5.

### Separate route, separate browser source

`/overlay/media` alongside `/overlay/alert`, as its own browser source in OBS. Sharing one page means a video and a follow alert fight over one DOM and one audio context; separate pages compose freely and can be positioned and layered independently in OBS.

### Serve clips from the backend, not the frontend

`frontend/public/media/` would work, and it's the wrong call: video files in git, and an `npm run build` every time you add a clip. Instead put them in a gitignored `backend/media/` behind a `StaticFiles` mount at `/media` — **registered above the SPA mount**, per the shadowing rule in `CLAUDE.md`. Drop a file in, reference it in config, no rebuild.

### The codec trap

**Encode as WebM (VP9 + Opus).** OBS's bundled CEF on Linux frequently ships without proprietary codecs, so an H.264/AAC MP4 can play silently, or not at all, *while working perfectly in your normal browser*. That asymmetry makes it a genuinely nasty afternoon to debug. Also tick **"Control audio via OBS"** on the browser source or the audio never reaches your mix.

### Queue on `ended`, not on a timer

The alert queue has to guess a duration from config. The media queue doesn't have to guess — chain on the `<video>` element's `ended` event, with a generous timeout as a backstop for a clip that fails to load or stalls. Same drain-loop shape as `AlertOverlay.vue`, so it'll read familiarly; the difference is what advances it.

Per-reward config decides **interrupt vs. queue**. A five-second airhorn should probably cut in; a thirty-second bit should wait its turn. This is the column people forget when they list their redeems.

Preload the next clip in the queue so there's no black gap between two back-to-back videos.

### While you're here: the panic button

One button on the dashboard that clears every queue and hides everything, on both overlays. When a clip is blaring at the wrong moment you do not want to be alt-tabbing to a terminal. It's twenty minutes of work and you will eventually be very glad it exists.

**Checkpoint:** a redeem plays its clip with audio in OBS. Two redeems fired back-to-back play in order rather than one stomping the other. The panic button kills a clip mid-playback.

---

## Phase 12 — The bot speaks (1–2 evenings)

Up to now the bot account has only owned the app registration — it does no work. Posting to chat needs a **second token, authorized as the bot**, with `user:write:chat` (plus `user:bot`, and `channel:bot` granted on your channel).

That means two identities in play, which is the real content of this phase:

| | Broadcaster token | Bot token |
|---|---|---|
| Authorized as | your main account | your bot account |
| Cached in | `.twitch_tokens.json` | `.twitch_bot_tokens.json` |
| Used for | reading follows, subs, bits, redeems, chat | *sending* chat messages |

Give the bot its own module — `backend/chat.py`, holding its own `Twitch` instance and its own `UserAuthenticationStorageHelper` — rather than growing `twitch.py` into something that manages two users. Inject it into `main.py` the same way `broadcast` is injected today.

Sending is a plain Helix call (`send_chat_message(broadcaster_id, sender_id, text)`). No IRC connection, no second websocket.

Two things to build in from the start: **rate limiting** (20 messages per 30 seconds for a normal account — a loop that trips this gets you timed out by Twitch, not just dropped), and **loop prevention** (never respond to messages from your own bot, or from other bots, or you'll find two bots talking to each other on stream at 2am).

**Checkpoint:** a button in `/control` posts a message to your channel as the bot, and a rate-limit test of 30 rapid messages degrades gracefully instead of erroring.

---

## Phase 13 — Loot boxes (2–3 evenings)

The first chat message a viewer sends each stream-day rolls them a random item, announced with an alert and a chat message. Like a Megabonk loot box, but the currency is showing up.

By this point the interesting parts already exist: chat events arrive (Phase 10's scope batch), the alert queue absorbs bursts (Phase 9's commit and the queue fix before it), and the bot can announce the drop (Phase 12).

### 13a — The roll, with no Twitch involved

New module `backend/loot.py`. A `POST /api/loot/roll` test endpoint runs the identical code path a real chat message will, so the entire feature is testable from the dashboard before a single chat event is wired up. Do this first; it makes 13b a twenty-line change.

- **Item pool in a committed `backend/loot.json`.** It's content, not settings — hand-edited, wants to be in git, gets long. Don't put eighty items through the config PUT payload.
- **Knobs in `config.json`** as a `LootConfig` model: enabled, reset hour, rarity weights, bot denylist.
- **Two-stage weighted roll** — pick rarity by weight, then an item uniformly within it. Weighting items individually means retuning odds requires touching every entry, and drift becomes invisible.
- **One `AlertKindConfig` per rarity** (`loot_common`, `loot_rare`, `loot_epic`, `loot_legendary`). Rarity → colour and jingle is exactly the mapping `kinds` already does, so a legendary sounds different from a common with no overlay code at all. Bonus: `/control` generates a test button per kind automatically.

### 13b — The day gate

Key on **`chatter_user_id`**, never the display name — names change case and get changed outright.

"Day" means **stream day, not calendar day**, or a stream crossing midnight re-rolls everybody:

```python
def stream_day(now: datetime, reset_hour: int = 6) -> str:
    return (now - timedelta(hours=reset_hour)).date().isoformat()
```

Persist claims to a gitignored `backend/loot_state.json`. Without persistence, a backend restart mid-stream hands out seconds. The file doubles as the data source for a future `!inventory`.

Bots claim loot unless you stop them: Nightbot, StreamElements, your own bot account. A lowercased denylist handles it. Decide explicitly whether `!command` messages count — I'd say yes, a lurker typing `!lurk` earned it.

### 13c — Wire the real chat event

`channel.chat.message` in `twitch.py`, keeping that module ignorant of loot by injecting an `on_chat` callback exactly as `broadcast` is injected. Announce the drop in chat via Phase 12.

**Checkpoint:** your first message of the day in your own chat rolls an item, shows a rarity-coloured alert in OBS, and posts to chat. Your second message does nothing. Restarting the backend doesn't let you roll again.

---

## The list after that

- **A history view, and SQLite.** Redeem history and loot inventories want to survive restarts and be queried. That's the point where JSON files stop being adequate — not before.
- Goal bars, ticker, chat display overlay (carried over from Part 1's fun list).
- Scene-aware widgets — react to OBS scene changes via simpleobsws events.
- Redeem moderation in `/control`: see the queue, replay a clip, cancel one.
- The OFL license text alongside `frontend/public/fonts/DepartureMono-Regular.otf`.
- Tests. There are none in either half. The alert and media queues are the first things here with logic worth pinning down.

## Rules of thumb, still

- One phase at a time, commit at every checkpoint.
- The standing gotchas — mount ordering, `StaticFiles` raising rather than returning 404, `<script setup lang="ts">`, stale `dist`, the Twitch CLI mock's `-u`/`-t` flags — live in `CLAUDE.md` and are kept current there. Read it before debugging something that feels impossible.
- When something breaks, check the browser devtools console *and* the FastAPI terminal. It's always in exactly one of them.
