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
Phase  9   Event delivery you can trust      DONE  ← infrastructure everything else leans on
Phase 10   Channel point redeems             DONE  ← new event source
Phase 11   The media overlay                 DONE  ← new output surface
Phase 12   The bot speaks                    DONE  ← new identity
Phase 13   Song requests                           ← chat becomes an input
Phase 14   M&M death counter                 DONE  ← (built out of order; see README)
Phase 15   Loot boxes                              ← composition of all of the above
```

**Phases 9–11 shipped 2026-09-15 and the project was tagged v1.0.** Notes on what differed from the
plan below, written after the fact:

- **Phase 10's re-auth:** the browser used whoever was already logged in, which was the *bot* account.
  That silently rejected every subscription. The token must be authorized as the broadcaster; the app
  registration living on the bot account is unrelated and normal.
- **Phase 11's codec trap is real and worth the warning it gets below.** The clips arrived as 11×
  H.264/AAC and one AV1, all transcoded to VP9/Opus before anything played.
- **Phase 11's stall backstop needs care.** Implemented first as a fixed 30s cap, it truncated a 75s
  clip at the halfway mark while the audio kept playing. It has to measure lack of progress, not
  elapsed time.
- **Interrupt-vs-queue:** the plan suggests short clips cut in. In practice losing 40s of a long clip
  to a 2s bit was the wrong trade, and every reward now queues. The field remains per-reward.

**Phase 12 shipped 2026-10-23**, verified live: the bot authorized and posted to chat. What differed:

- **The token file is `.chat_tokens.json`,** not `.twitch_bot_tokens.json` as planned below. The table
  has been corrected.
- **`user:bot` and `channel:bot` aren't needed.** Those are for sending with an *app* token. A user
  token sending as itself needs only `user:write:chat`.
- **Switching accounts was easy:** twitchAPI's auth helper forces Twitch's consent page, and its
  "Not you?" link switches to the bot. Startup warns if the bot's id equals the channel's.
- **The rate limiter drops, it doesn't queue,** and it counts attempts rather than successes, because
  Twitch counts what it's sent. Twitch's own `drop_reason` is passed back as an error, never reported
  as a send.
- **Loop prevention is deferred to Phase 13** (song requests), the first phase that reads chat. Until then there's nothing to loop on.
  `ChatBot.bot_user_id` is there for that check.
- **A `"chat"` reward action** isn't routed yet.

**Security pass, same day.** Once the API could post as the bot, "no login" stopped being harmless.
`fastapi run` listens on every interface, and this machine's firewall zone allows ports 1025–65535,
so anyone on the same Wi-Fi could drive OBS, post to chat, or play any URL as a clip on stream.
Worse, any web page open in your own browser could do it too: body-less POSTs need no CORS
preflight, and websockets ignore CORS entirely. Now:

- `stream.sh` binds `127.0.0.1`.
- `TrustedHostMiddleware` blocks DNS rebinding.
- A non-local `Origin` is refused on writes and on the `/ws` handshake.
- `/ws` relays only an allow-list of types, and only `/media/` clip sources.

No secret had ever been committed; checked against the full history before the repo went further.

The order is deliberate. Each phase is worth more once the one before it exists, and loot boxes (Phase 15) are last precisely *because* they're the least foundational — they're the payoff, not the plumbing.

**One cross-cutting constraint, read this before Phase 10:** every change to the OAuth scope list invalidates the cached token in `backend/.twitch_tokens.json` and forces a browser re-authorize. Phase 10 needs `CHANNEL_READ_REDEMPTIONS`, and Phases 13 and 15 need `USER_READ_CHAT`. **Add both at once in Phase 10**, even though you won't use the second for weeks. One re-auth instead of two.

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
| Cached in | `.twitch_tokens.json` | `.chat_tokens.json` |
| Used for | reading follows, subs, bits, redeems, chat | *sending* chat messages |

Give the bot its own module — `backend/chat.py`, holding its own `Twitch` instance and its own `UserAuthenticationStorageHelper` — rather than growing `twitch.py` into something that manages two users. Inject it into `main.py` the same way `broadcast` is injected today.

Sending is a plain Helix call (`send_chat_message(broadcaster_id, sender_id, text)`). No IRC connection, no second websocket.

Two things to build in from the start: **rate limiting** (20 messages per 30 seconds for a normal account — a loop that trips this gets you timed out by Twitch, not just dropped), and **loop prevention** (never respond to messages from your own bot, or from other bots, or you'll find two bots talking to each other on stream at 2am).

**Checkpoint:** a button in `/control` posts a message to your channel as the bot, and a rate-limit test of 30 rapid messages degrades gracefully instead of erroring.

---

## Phase 13 — Song requests (3–4 evenings)

A viewer types `!sr <youtube link>` in chat. The bot confirms with the title and the viewer's queue position. The song plays on stream in its own OBS browser source, with a "now playing" card. Mods can `!skip`, and you run the queue from `/control`.

This is the first phase where **chat becomes an input**, and it's the real content of the phase. Until now every input was either you (the dashboard), Twitch on a viewer's behalf (follows, subs, redeems) or the game. A chat message is arbitrary text from anyone, so everything below is built around never trusting it.

### Read this first: copyright

Twitch's music rules apply to *your* channel, whoever requested the song. Copyrighted music playing live can get the stream muted or earn you a DMCA strike. It also mutes the VOD and every clip made from it. A song-request feature with an open YouTube search box is the classic way streamers collect strikes. Mitigations, and none of them is a guarantee:

- **Route the song source off the VOD track.** In OBS: Settings → Output → Advanced, then the Twitch VOD Track. Viewers still hear songs live, but the archive stays clean. Check that it really excludes the source with a test VOD.
- **Have an allowlist mode from the start.** A `songs.allow_channels` list (NCS, Monstercat Uncaged, StreamBeats, your own uploads) that you can switch on in one click, if this ever bites.
- **The off switch is a dashboard toggle, not a code change.**

### 13a — The queue, with no Twitch involved

Same trick as loot boxes: a `POST /api/songs/request {url, user}` test endpoint runs the identical code path `!sr` will, so the whole feature is testable from the dashboard first.

New module `backend/songs.py`, holding `SongQueue`. Its dependencies are injected (`broadcast`, the lookup, later the bot's `send`), the way `twitch.py` is.

**The queue lives on the backend, unlike the alert queue.** The alert queue is a display concern, so it lives in the overlay. A song queue has several readers and writers: `!queue` in chat, Skip in `/control`, the overlay reporting a song ended. That needs one authoritative copy. Persist it to a gitignored `backend/songs.json`, so a restart mid-stream doesn't throw away twenty requests. The *playback position* isn't persisted; a restarted song starts from the top.

**The URL never leaves the backend; only an 11-character video id does.** Pull the id out with one regex over the link shapes people actually paste:
- `youtube.com/watch?v=`
- `youtu.be/`
- `/shorts/`
- `music.youtube.com`
- `m.youtube.com`

An 11-character `[A-Za-z0-9_-]` id is also accepted on its own. Reject anything else. The overlay builds the embed itself from the id. That way the security pass's rule ("the media overlay only plays `/media/`") has a twin here: the song overlay only ever loads `youtube.com/embed/<id>`, and a crafted message can't point it anywhere else.

**Look up every request before queueing it**, using the YouTube Data API v3. `videos.list?part=snippet,contentDetails,status&id=<id>` costs 1 quota unit out of a free 10,000 a day. One call answers every question the queue needs:

| Field | Used for |
|---|---|
| `snippet.title`, `snippet.channelTitle` | the bot's reply, the now-playing card, the channel allowlist |
| `contentDetails.duration` (ISO 8601, `PT4M13S`) | the length cap |
| `status.embeddable` | refuse up front instead of failing on stream |
| `snippet.liveBroadcastContent` | refuse live streams, which never end |

The API key goes in `.env` as `YOUTUBE_API_KEY`. It's a plain key, not OAuth, so no third browser authorization is needed. Text search (`!sr never gonna give you up`) is `search.list` at **100 units**, so roughly 100 searches a day. Leave it out of the first version; links only.

**Knobs in `config.json`** as a `SongConfig` model:
- `enabled`, flipped from the dashboard and announced in chat when it changes;
- `max_duration_s` (6 minutes is a sane default);
- `max_queue`;
- `per_user_limit` (one song in the queue per viewer);
- `who`: everyone / subs / VIPs and mods;
- `blocklist`, a list of video ids and channel ids.

**Checkpoint:** from `/control`, request a normal song, a 40-minute video, an embedding-disabled video and garbage text. The first queues, the other three are refused with a reason, and the queue survives a backend restart.

### 13b — The player overlay

A new page, `/overlay/song`, as its own OBS browser source, for the same reason media has its own: separate DOM, separate audio. Tick "Control audio via OBS" so it gets its own mixer fader.

Use the **YouTube IFrame Player API** (`https://www.youtube.com/iframe_api`, then `new YT.Player(...)`), not a bare `<iframe>`. You need its events:
- `onStateChange` with `YT.PlayerState.ENDED` → the song finished;
- `onError` → skip it. Code `2` is a bad id, `5` is a player error, `100` means removed or private, and `101`/`150` mean the owner disabled embedding. **Expect 150 constantly**: label-owned music videos often block embedding even when `status.embeddable` said yes, or the restriction only applies in some regions.

**The overlay reports back; the backend decides what's next.** On `ENDED` or `onError`, the overlay calls `POST /api/songs/ended {request_id}` (same origin, so the Origin guard is happy). The backend advances the queue and broadcasts the new `type: "song"` state. Carry the `request_id` and ignore stale ones. Otherwise two copies of the overlay open at once, say OBS and a browser tab, would each report the end, and the queue would skip a song.

**Apply the media lesson.** No fixed timeout on playback: a 30-second cap cut a 75-second clip in half in Phase 11, and it would do the same to every song. The stall check must measure *lack of progress*, polling `getCurrentTime()` every few seconds, and skip only when it stops advancing while the player state still says playing.

**On a reconnect, ask for the state instead of trusting replay.** Like the zone banner, the song overlay ignores `replay: true` messages. On connect it calls `GET /api/songs/now` and resumes that song, so a refreshed browser source doesn't replay a backlog of "now playing" messages.

The **now-playing card** goes on the same page, so it can never disagree with what's audible. It shows the title, the channel and "requested by <name>", rendered through Vue interpolation as usual. Titles are viewer-chosen text; never `v-html` them.

**Test YouTube in OBS specifically, not just in a browser.** The CEF codec trap from Phase 11 applies. YouTube normally serves VP9/Opus to a browser without H.264, which should be fine, but prove it in the browser source with sound before trusting it. Some embeds also refuse to play without a referrer or origin; pass `origin: 'http://localhost:8000'` in `playerVars`.

**Checkpoint:** two queued songs play back to back in OBS with audio. Skip from `/control` cuts to the next one. An embed-blocked video is skipped within a second or two. Panic stops music too.

### 13c — Reading chat

`channel.chat.message` via `listen_channel_chat_message(broadcaster_id, user_id, callback)` in `twitch.py`. Pass your id for both. It uses the broadcaster token and `user:read:chat`, **which Phase 10's scope batch already granted, so there's no re-authorize**. That's what the batching was for.

Keep `twitch.py` ignorant of songs. Inject a single `on_chat` callback the way `broadcast` is injected, and have `main.py` dispatch commands to handlers. Loot boxes (Phase 15) become another subscriber to the same dispatcher, not a second chat subscription.

**Loop prevention lives here**, the item Phase 12 deferred. Drop any message whose `chatter_user_id` is the bot's (`chat_bot.bot_user_id`). Also drop anything from a lowercased bot denylist (Nightbot, StreamElements, Moobot, ...), before any command parsing. Key everything on `chatter_user_id`, never the display name.

**Permissions come from the event's `badges`** (`broadcaster`, `moderator`, `vip`, `subscriber`), not from extra Helix lookups. Commands:
- **`!sr <link>`** for everyone, or whatever `who` allows;
- **`!song`**: what's playing;
- **`!queue`**: the next few songs and your own position;
- **`!wrongsong`**: remove your own most recent request;
- **`!skip`**: mods and broadcaster only.

**Replies thread** with `reply_parent_message_id`, the event's `message_id`. That needs one optional parameter added to `ChatBot.send`.

**Mind the bot's rate limit.** Twenty replies per 30 seconds sounds generous until a chat of 40 people spams `!sr` at once. When the limiter drops a reply, the request must still queue; a missing confirmation is cosmetic, a missing song isn't. Bot messages also pass through AutoMod and the channel's settings. A title with a blocked term comes back as a `drop_reason`, which `ChatBot.send` already reports.

**Respect mod deletions:** `listen_channel_chat_message_delete` fires when a mod removes a message. If that message was an `!sr`, pull the request. That's cheap, and it's what mods will assume happens.

**Checkpoint:**
- `!sr <link>` from a second account queues, gets a threaded bot reply, and plays.
- `!sr` from the bot's own account is ignored.
- A non-mod `!skip` does nothing; a mod `!skip` works.
- A mod deleting an `!sr` message removes that request.
- Thirty rapid `!sr`s from different accounts all queue (up to `max_queue`), even though some replies get rate-limited.

### 13d — The dashboard

A Song Requests card in `/control`. Requests open/closed toggle, the now-playing song, the queue with remove buttons, skip, and a volume slider (`player.setVolume`, broadcast to the overlay; the OBS fader still sits on top). It's ordinary work at this point; nothing new is learned here, which is why it's last.

---

## Phase 14 — M&M death counter

Built ahead of Phases 12, 13 and 15, out of order, and documented in `CLAUDE.md` and `learning/overlays.md` rather than here.

---

## Phase 15 — Loot boxes (2–3 evenings)

The first chat message a viewer sends each stream-day rolls them a random item, announced with an alert and a chat message. Like a Megabonk loot box, but the currency is showing up.

By this point the interesting parts already exist: chat is read and dispatched with loop prevention (Phase 13), the alert queue absorbs bursts (Phase 9's commit and the queue fix before it), and the bot can announce the drop (Phase 12).

### 15a — The roll, with no Twitch involved

New module `backend/loot.py`. A `POST /api/loot/roll` test endpoint runs the identical code path a real chat message will, so the entire feature is testable from the dashboard before a single chat event is wired up. Do this first; it makes 15b a twenty-line change.

- **Item pool in a committed `backend/loot.json`.** It's content, not settings — hand-edited, wants to be in git, gets long. Don't put eighty items through the config PUT payload.
- **Knobs in `config.json`** as a `LootConfig` model: enabled, reset hour, rarity weights, bot denylist.
- **Two-stage weighted roll** — pick rarity by weight, then an item uniformly within it. Weighting items individually means retuning odds requires touching every entry, and drift becomes invisible.
- **One `AlertKindConfig` per rarity** (`loot_common`, `loot_rare`, `loot_epic`, `loot_legendary`). Rarity → colour and jingle is exactly the mapping `kinds` already does, so a legendary sounds different from a common with no overlay code at all. Bonus: `/control` generates a test button per kind automatically.

### 15b — The day gate

Key on **`chatter_user_id`**, never the display name — names change case and get changed outright.

"Day" means **stream day, not calendar day**, or a stream crossing midnight re-rolls everybody:

```python
def stream_day(now: datetime, reset_hour: int = 6) -> str:
    return (now - timedelta(hours=reset_hour)).date().isoformat()
```

Persist claims to a gitignored `backend/loot_state.json`. Without persistence, a backend restart mid-stream hands out seconds. The file doubles as the data source for a future `!inventory`.

Bots claim loot unless you stop them: Nightbot, StreamElements, your own bot account. A lowercased denylist handles it. Decide explicitly whether `!command` messages count — I'd say yes, a lurker typing `!lurk` earned it.

### 15c — Wire the real chat event

Phase 13 already reads `channel.chat.message` and dispatches it, with bots and the bot's own messages dropped. Loot is one more handler on that dispatcher, which sees *every* message, not just `!commands`. Announce the drop in chat via Phase 12. The Phase 13 bot denylist covers "bots claim loot", so 15b's separate denylist can just reuse it.

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
