# The Overlays

A field guide to every browser source sang_suite puts on stream: what each one shows, what sets it off,
how to add it to OBS, and how to tune it. For how the machinery underneath works, see
[`how-it-works.md`](how-it-works.md).

| Overlay | URL | OBS size | Fired by |
|---|---|---|---|
| [Alerts](#alerts) | `/overlay/alert` | 1920 × 1080 | Twitch events, manual alerts, M&M level-ups |
| [Media](#media) | `/overlay/media` | 1920 × 1080 | Channel point redeems |
| [Death counter](#death-counter) | `/overlay/deaths` | 420 × 180, a corner | M&M deaths, from the Ledger |
| [Zone banner](#zone-banner) | `/overlay/zone` | 1920 × 300, bottom edge | M&M zone changes, from `Player.log` |

Every URL is on `http://localhost:8000` when streaming (`./stream.sh`), or `:5173` while developing with
Vite. Each overlay is its own page and its own browser source, so they layer independently in OBS and
never fight over one screen or one audio context.

---

## What they all share

- **Transparent.** Each page draws only its widget; everything else shows the scene beneath.
- **Driven by one WebSocket.** The backend broadcasts every event on `/ws`, and each overlay picks out
  the message `type` it cares about and ignores the rest.
- **Nothing lost to a blip.** Events carry a sequence id; an overlay that reconnects asks for what it
  missed and gets it replayed. (The zone banner deliberately skips replays — a banner for a zone you've
  since left would just be wrong.)
- **Self-healing.** If the backend restarts, overlays reconnect on their own within a couple of seconds.
- **Panic.** The panic button on `/control` clears every queue and blanks every overlay at once.

**OBS settings for every source:** tick **Shutdown source when not visible** and **Refresh browser
when scene becomes active** — OBS caches page assets hard, and these spare you clearing the cache by
hand after an update. Tick **Control audio via OBS** on any overlay that makes sound (alerts, media,
death counter) or it never reaches your mix.

---

## Alerts

**`/overlay/alert` · 1920 × 1080**

A headline and a line of text slide in with a per-kind accent colour and a synthesised jingle, hold, and
leave. Several arriving together queue up rather than overwrite each other.

**What fires it**

| Kind | Source | Example |
|---|---|---|
| `follow` | Twitch | New Follower: Innoruuk |
| `sub` / `resub` | Twitch | Innoruuk subscribed! (Tier 1) |
| `cheer` | Twitch | Innoruuk cheered 500 bits! |
| `raid` | Twitch | incoming raid with viewer count |
| `redeem` | Twitch channel points, for rewards whose `actions` include `alert` | |
| `level_up` | **Monsters & Memories Ledger** | Sanguine reached level 20 |
| `generic` | the manual alert box on `/control` | anything you type |

**Tuning it** — on `/control` → *Alert Settings*: alert duration, volume, and each kind's headline,
accent colour and sound. Saving pushes the change to the overlay live; no refresh. Every kind has a
test button.

**Good to know**

- Only *your own* level-ups alert. Party members' level-ups are recorded, but don't go on stream.
- Adding a new alert kind takes two edits: `DEFAULT_KINDS` in `backend/main.py` **and**
  `backend/config.json`, because the file replaces the defaults wholesale.

---

## Media

**`/overlay/media` · 1920 × 1080 · tick *Control audio via OBS***

Plays a clip or gif when a viewer redeems a channel point reward routed to it.

**What fires it** — a redeem whose reward in `config.json` has `"media"` in its `actions` and a
`media` path, e.g. `"videos/bait.webm"` (files live in `backend/media/`).

**Behaviour**

- Clips queue and play in order, each to the end — there's deliberately no time cap.
- A reward with `"interrupt": true` cuts in instead of waiting its turn.
- Gifs and webp loop forever with no "ended" event, so they show for a fixed time.

**Good to know** — encode clips as **WebM (VP9 + Opus)**. OBS's browser on Linux often lacks
H.264/AAC, so an MP4 can be silent or blank in OBS while playing fine in your own browser. The command
is in the README under *Clips and gifs*.

---

## Death counter

**`/overlay/deaths` · 420 × 180, positioned in a corner**

An Underdark grimoire plaque: a Lolth spider sigil in a slowly turning ring of runes, a candle-lit
blackletter count of this stream's deaths, and the zone of the last one. Each death wrenches the
ring, flares the plaque, and tolls a bell.

**What fires it** — `act_02` events in the game's Ledger
(`<settings>/<server>/<Character>/Ledger/*_Character_*.json`), polled every 2 seconds.

**Controls** — the *Death Counter* card on `/control`:

- **New Stream** — back to 0, counting from now. Press it when you go live. (A session older than 6
  hours is also replaced automatically when the backend starts.)
- **−1 / +1** — manual correction when the Ledger gets it wrong.
- **Test Death** — previews the flare and bell on a fake +1 that never touches the real count.

**Good to know** — the count is never stored, only recomputed from the Ledger's timestamps, so
restarting the backend mid-stream can't lose or double it.

---

## Zone banner

**`/overlay/zone` · 1920 × 300, pinned to the bottom edge of the canvas**

A lower-third that rises out of the dark when you enter a zone: the zone's name in Cinzel, a violet
rule with a small lozenge, and an optional line of lore in italic. It holds about six seconds and sinks
away. Zoning again mid-banner restarts it with the new name. It makes no sound — the game's own zone
music has that covered.

**What fires it** — the game's `Player.log`, not the Ledger, because the Ledger only notices a zone once
something happens in it. The backend (`backend/zones.py`) watches for

```
[Client][ZONE] Start zoning process to nightharbore      <- remembers where you're going
[Client][ZONE] Done waiting, hide LoadingPanel           <- banner goes up now
```

and fires on the second line, so the banner appears as the loading screen clears instead of behind it.
Logging in counts as zoning in, so you get a banner for your starting zone too.

**Names and lore — `backend/zones.json`**

```json
"nightharbore": { "name": "Night Harbor East", "lore": "Where the tide brings in the drowned." }
```

- The key is the game's internal zone name, as it appears in `Player.log`.
- `name` is what the banner shows. A zone missing from the file (or with an empty name) falls back to
  its internal name title-cased — "Minicrypt" — so a new zone still gets a banner.
- `lore` is optional; leave it `""` and the banner shows just the name. **All lore lines start empty —
  they're yours to write.**
- The file is re-read on every zone change: edit, save, and the next zone uses it. No restart.

**Controls** — **Test Zone** on the *Death Counter* card previews the banner for the zone you're in
right now, with its real name and lore — the quickest way to check an edit to `zones.json`.

**Good to know** — on startup the watcher reads the existing log only to learn where you are
(`GET /api/zone`); it never replays the day's zoning. The game starts a fresh `Player.log` every launch,
and the watcher follows it across.

---

## Behind the M&M overlays: the Ledger ingester

`backend/ledger.py` isn't an overlay, but it feeds them. Every 2 seconds it reads all of the game's
Ledger files — your Character log, the Social log (which also records your party members), and the
archives — and keeps every event, decoded, in `backend/ledger.db` (SQLite, gitignored). The history
survives even if the game later prunes its archives.

What it records: loot (with the mob it came from), coin looted from corpses, harvests, vendor sales,
trades, drops and destroys, level-ups, deaths, and party joins, leaves and leader changes — each with
zone, level and time.

Today it drives the `level_up` alert. It also answers two queries, ready for future overlays like an
end-of-stream recap card:

- `GET /api/ledger/summary` — this stream's totals: what you looted and from whom, coin earned,
  harvests, zones in the order you visited them, level-ups, and who you grouped with. Defaults to "since
  New Stream"; `?since=2026-10-13T18:00` and `?owner=Sanguine` narrow it.
- `GET /api/ledger/recent?kind=loot&limit=20` — the latest events, optionally of one kind.

---

## When something doesn't show up

| Symptom | Check |
|---|---|
| Nothing at all on any overlay | Is the backend running? The `/control` header shows backend and WebSocket status. Did you rebuild (`./stream.sh` does it for you)? |
| Overlay shows old styling | Refresh the browser source in OBS, or tick *Refresh browser when scene becomes active*. |
| Sound missing from the recording | *Control audio via OBS* on that source. |
| Clip silent or blank in OBS only | Re-encode to WebM — see Media. |
| Death counter says "M&M not found" | The game's settings folder wasn't found. Set `MNM_SETTINGS_DIR` in `backend/.env`. |
| No zone banner | Use **Test Zone** first: if that works, the overlay is fine and the backend isn't seeing `Player.log` (`GET /api/zone` shows what it's watching). |
| Level-up alert missing | Check `level_up` exists under *Alert Settings*. If you saved settings from a dashboard opened before the update, the kind may have been overwritten — restart the backend and re-add it. |
