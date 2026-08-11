# How sang_suite Works

A plain-language tour of the whole project, with the vocabulary spelled out. Every example is real code
from this repo, so you can open the file and follow along.

Read it top to bottom once. After that it works as a reference — the section headings match the files.

**The map, in one picture:**

```
              you, in a browser                     OBS
            ┌──────────────────┐         ┌────────────────────────┐
            │  /control        │         │  browser source →      │
            │  (the dashboard) │         │  /overlay/alert        │
            └────────┬─────────┘         └───────────┬────────────┘
                     │  WebSocket /ws                │  WebSocket /ws
                     └───────────────┬───────────────┘
                                     ▼
                        ┌────────────────────────┐
                        │   FastAPI  :8000       │
                        │   backend/main.py      │
                        │                        │
                        │  • /api routes         │
                        │  • /ws relay (server)  │
                        │  • config.json         │
                        │  • serves the built UI │
                        └───┬────────────────┬───┘
                            │                │
             dials OUT ─────┘                └───── dials OUT
                     ▼                              ▼
        ┌────────────────────┐          ┌────────────────────────┐
        │ OBS WebSocket:4455 │          │ Twitch EventSub        │
        │ (scenes, sources)  │          │ (follows, subs, …)     │
        └────────────────────┘          └────────────────────────┘
```

Everything below is an expansion of that picture.

---

## 1. The 10,000-foot view

**The restaurant analogy.** Your app is a restaurant:

- The **frontend** (Vue) is the *dining room* — everything the customer sees and touches.
- The **backend** (FastAPI) is the *kitchen* — does the real work, out of sight.
- **Vite** is the *waiter* — the dining room never walks into the kitchen. It hands orders to the waiter,
  who carries them back.

That last part is why, **while you're developing**, both servers must be running. Your browser only ever
talks to Vite (port 5173). When it asks for something starting with `/api` or `/ws`, Vite quietly forwards
it to FastAPI (port 8000) and brings the answer back. That forwarding rule lives in `frontend/vite.config.ts`:

```ts
proxy: {
  '/api': 'http://localhost:8000',
  '/ws': { target: 'ws://localhost:8000', ws: true },
}
```

If the kitchen is closed (backend not running), the waiter comes back with `ECONNREFUSED` — that's the
error that spams the Vite console.

**But on stream, the waiter goes home.** `./stream.sh` builds the frontend into plain files and hands them
to FastAPI to serve directly, so there's one process on one port and no Vite at all. Same app, one fewer
moving part. Section 9 covers this properly — for now just hold onto: *two servers while coding, one while
streaming.*

---

## 2. Vocabulary cheat sheet

Terms that show up constantly. Skim now, refer back later.

### Data shapes

| Term | What it means | Example from your code |
|---|---|---|
| **Object** (JS) / **dict** (Python) | A labeled bag of values. Same idea, different language's word. | `{ type: 'alert', text: 'New Follower' }` |
| **Key / value pair** | One label and its contents. The unit an object is made of. | `type` is the key, `'alert'` is the value |
| **Property** | A key on an object, once it's attached. "The object *has a* `type` property." | `event.type` |
| **Attribute** | Python's word for a value stored on an object. Reached with a dot. | `self._url`, `resp.responseData` |
| **JSON** | A *text* format for sending objects between programs. The shared language of your two halves. | `{"type":"alert","text":"..."}` |
| **Serialize / deserialize** | Turning an object into JSON text, and back. | `JSON.stringify(event)` / `json.loads(...)` |

> **Object vs JSON** — an object lives *in memory* in one program. JSON is what it becomes when you need to
> *ship it over the wire*. Same content, different state of matter: ice vs water.

### Code shapes

| Term | What it means | Example |
|---|---|---|
| **Function** | A named, reusable block of steps. | `function connect() { ... }` |
| **Parameter** | The name in the definition — the empty slot. | `def health()`, `path` in `callObs(path, body)` |
| **Argument** | The real value you pass in. | `callObs('/api/obs/scene', {...})` |
| **Return value** | What the function hands back. | `return {"status": "ok"}` |
| **Class** | A blueprint. | `class OBSController:` |
| **Instance** | One actual thing built from the blueprint. | `obs = OBSController(...)` |
| **Method** | A function that belongs to a class. | `obs.call(...)` |
| **`self`** | Inside a class, "this particular instance." | `self._client` |
| **Decorator** | A `@line` above a function that attaches extra meaning. | `@app.get("/api/health")` |
| **Callback** | A function you hand to someone else to run *later*, when something happens. | `socket.onmessage = (e) => {...}` |

> **Blueprint vs building.** `class OBSController` is the blueprint — it describes what an OBS controller
> *would* have. `obs = OBSController(...)` constructs the actual building. You can make many buildings from
> one blueprint; here you only need one.

> **The leading underscore.** `self._client`, `_ready_client`, `_load_env` — Python has no real "private",
> so an underscore is a note to the reader: *this is internal plumbing, don't poke it from outside.* Nothing
> enforces it. It's a "staff only" sign, not a locked door.

### Async

| Term | What it means |
|---|---|
| **`async def`** | This function does slow things (network, disk) and can be paused. |
| **`await`** | "Pause here until this finishes — and let other work run meanwhile." |
| **Task** | Work started with `asyncio.create_task(...)` that runs *alongside* you instead of blocking you. |

> **The waiter analogy again.** A waiter who `await`s doesn't stand frozen at the kitchen door while your
> steak cooks. They go take three other tables' orders and come back when it's ready. That's why your
> backend can serve many overlay pages at once without freezing.

### Settings words

| Term | What it means | In this project |
|---|---|---|
| **Environment variable** | A value handed to a program by the system it runs in, not by its code. | `OBS_WS_PASSWORD` |
| **`.env` file** | A text file of environment variables, kept out of git. | `backend/.env` (secrets) |
| **Pydantic model** | A Python class describing the *shape* of some data, which then validates it. | `class AlertKindConfig(BaseModel)` |
| **Validation** | Checking incoming data matches that shape before your code touches it. | rejecting `duration_ms: 99999999` |

> **Two kinds of settings, deliberately kept apart.** `.env` holds *secrets and wiring* — passwords, client
> IDs, ports. It is read once at startup and never written. `config.json` holds *stuff you tweak while
> streaming* — alert colours, durations, volume — and the control panel writes it. Never put a password in
> `config.json`; it's a file the UI can overwrite.

---

## 3. The backend — `backend/main.py`

One file, and the order of it matters. Reading top to bottom: settings, then the OBS client, then startup,
then routes, then — last of all — the static file mount. That last one is positional, not stylistic
(section 3.6).

### 3.1 Reading `.env` by hand

```python
def _load_env(path: str = ".env") -> None:
    ...
    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
```

A twelve-line reader instead of the `python-dotenv` package — one fewer dependency for something this
small. `setdefault` is the interesting choice: it only sets the variable **if it isn't already set**, so a
value you export in your shell beats the file. That's the conventional precedence, and it means you can
override one setting for one run without editing anything.

```python
OBS_WS_PASSWORD = os.getenv("OBS_WS_PASSWORD")  # None = connect without auth
```

`os.getenv` returns `None` when a variable is missing, which is exactly what `simpleobsws` wants for "no
password." Missing config degrades into a sensible default rather than a crash — a theme you'll see again.

### 3.2 The settings models — Pydantic

```python
class AlertKindConfig(BaseModel):
    label: str
    accent: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    sound: str = "levelup"
```

**The form-with-rules analogy.** A Pydantic model is a form that checks itself. `label` must be text.
`accent` must match that pattern — a `#` and exactly six hex digits — so `"reddish"` is rejected at the
door. `sound` has a default, so an older `config.json` written before that field existed still loads.

The rules aren't decoration; each one prevents a specific failure:

| Rule | Stops |
|---|---|
| `accent` pattern | A colour the overlay's hex→rgb maths would turn into `NaN` |
| `duration_ms` `ge=500, le=60_000` | A typo pinning an alert on screen for the rest of the stream |
| `volume` `ge=0.0, le=1.0` | Gain above 1.0, which clips into distortion |

Because these models describe both what's on disk *and* what the API accepts, one definition gives you
validation in both directions for free — that's the whole reason FastAPI leans on Pydantic.

```python
def load_config() -> Config:
    if CONFIG_PATH.exists():
        try:
            return Config.model_validate_json(CONFIG_PATH.read_text())
        except (ValueError, OSError) as exc:
            print(f"[config] {CONFIG_PATH.name} unusable, using defaults: {exc}")
    return Config()
```

Note what a broken file does: prints a complaint, returns defaults, **keeps booting**. Mid-stream is the
worst possible time for a server to refuse to start over a stray comma.

### 3.3 Job one: answer HTTP requests

```python
@app.get("/api/health")
async def health():
    return {"status": "ok"}
```

- `@app.get("/api/health")` is a **decorator** — a *sign hung on a door*. It tells FastAPI: "when a GET
  request arrives for this path, run the function below."
- The function returns a Python **dict**; FastAPI **serializes** it to JSON automatically.
- Every path starts with `/api` so the Vite waiter knows to forward it. A route without that prefix would
  never be found by the browser in dev — and in production it would collide with the static file mount.

### 3.4 Job two: relay alerts (`ConnectionManager`)

```python
class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []
```

**The group-chat analogy.** `ConnectionManager` keeps a guest list of everyone currently connected — each
open overlay page, plus your control panel. When a message arrives, `broadcast` sends it to everyone on
the list.

- `self.connections` is an **attribute**: a list living on the instance.
- `connect` adds someone to the list, `disconnect` removes them, `broadcast` messages everybody.

The important consequence: it broadcasts to whoever is on the list **right now**. Anyone who connects a
second later never sees it. There's no recorded history — it's a live radio broadcast, not a podcast.

Two consequences you can now see in the UI:

- The relay echoes to **everyone including the sender**, which is why an alert you fire from the dashboard
  shows up in that dashboard's own Activity feed.
- The feed in the store is the *only* history that exists, and it lives in one browser tab. Refresh the
  page and it's gone. If you ever want a real log, it has to live on the backend.

### 3.5 Job three: control OBS (`OBSController`)

This one dials *out* to OBS. Its whole job is managing one attribute, `self._client`, which is either
`None` (no connection) or a live connection.

**The phone-line analogy:**

| Method | What it does | In the analogy |
|---|---|---|
| `__init__` | Stores the URL and password. Connects to nothing. | Writing the number on a sticky note |
| `_ready_client` | Returns a working connection, dialing fresh only if needed | "Is the line still up? If yes, use it. If not, redial." |
| `call` | Sends one request, converts failures into HTTP errors | Speaking into the phone |
| `disconnect` | Closes it cleanly at shutdown | Hanging up |

The clever bit is in `_ready_client`:

```python
if self._client is not None and self._client.is_identified():
    return self._client
```

*If I already have a live line, reuse it.* Only when that check fails does it pay the cost of dialing
again. This is why the first button press is slower than the rest.

And in `call`, when the connection is dead:

```python
self._client = None
raise HTTPException(status_code=503, detail=...)
```

Setting `self._client = None` **throws away the dead line** so the next attempt redials instead of
shouting into a disconnected phone. That's the self-healing part.

**Status codes are diagnostic.** They tell you which step went wrong:

| Code | Meaning | Fix |
|---|---|---|
| `503` | Can't reach OBS at all | Start OBS / enable its WebSocket server / check `.env` |
| `502` | OBS answered but said no | You misspelled the scene or source name |
| `200` | Worked | 🎉 |

### 3.6 Startup and shutdown — `lifespan`

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... everything before `yield` runs at startup
    yield
    # ... everything after runs at shutdown
```

**The opening-and-closing-shift analogy.** Before `yield` is unlocking the doors and turning the lights on;
after `yield` is cashing out and locking up. FastAPI runs the first half before it accepts any request and
the second half on Ctrl-C.

Startup does three things, and *all three are allowed to fail*:

```python
try:
    await obs._ready_client()
    app.state.refresh_task = asyncio.create_task(_refresh_once_serving())
except (ConnectionError, OSError):
    pass
try:
    await twitch_alerts.start()
except Exception as exc:
    print(f"[twitch] Startup failed, alerts disabled: {exc}")
```

OBS not running? Fine, the connection is lazy anyway. Twitch credentials missing? Fine, no automatic
alerts. Neither can stop the server booting — because the one thing that must always work is serving the
overlay.

The refresh task is worth understanding, because it solves a real annoyance. If OBS was already open when
you start the backend, its browser sources tried to load a dead port and are stuck on Chromium's error
page. No JavaScript ever ran there, so the store's reconnect loop can't rescue them — the page is simply
dead until something reloads it. `refresh_overlay_sources` presses OBS's own *"Refresh cache of current
page"* button for you, via the OBS websocket, on any browser source whose URL points at `localhost:8000`.

Two subtleties in that code, both the sort of thing that bites once and then never again:

```python
for _ in range(50):
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", OVERLAY_PORT)
```

1. **Why poll our own port first?** Uvicorn runs `lifespan` startup *before* it binds the listening socket.
   Refreshing straight away would reload OBS into another connection-refused. So the task waits until it
   can connect to itself — proof that we're actually serving — then refreshes. Up to ~10s, after which
   something else is wrong.
2. **Why `app.state.refresh_task = ...` instead of a bare `create_task(...)`?** asyncio only keeps a *weak*
   reference to running tasks. If nothing else holds onto it, the garbage collector is entitled to eat it
   mid-flight. Parking it on `app.state` keeps a strong reference alive, and gives shutdown something to
   `.cancel()`.

### 3.7 Serving the built frontend — and why the mount is last

```python
if DIST.is_dir():
    app.mount("/", SPAStaticFiles(directory=DIST, html=True), name="spa")
```

A mount at `/` matches *everything*. Routes are checked in declaration order, so anything declared **after**
this line is unreachable — the mount swallows it first. Hence the rule in `CLAUDE.md`: **new routes go
above the mount.** In development the directory doesn't exist at all and the whole block is skipped, which
is why you never noticed it while working on Vite.

```python
class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if path.startswith("api/"):
                raise
            return await super().get_response("index.html", scope)
```

`/control` and `/overlay/alert` are **client-side routes** — vue-router invents them in the browser, and
there is no such file on disk. A plain static mount 404s on a hard refresh. Handing unknown paths
`index.html` lets the Vue app boot and sort the URL out itself.

Two details worth noticing:

- Starlette's `StaticFiles` signals "no such file" by **raising** an exception rather than returning a 404
  response. That's why this is a `try/except` and not an `if response.status_code == 404`.
- The `api/` check keeps that fallback away from API paths. Without it, a typo'd endpoint returns a page of
  HTML, and your `res.json()` fails with a baffling *"Unexpected token '<'"* instead of a clean 404.

---

## 4. The Twitch half — `backend/twitch.py`

The rest of the backend is one file; Twitch got its own because it's the one part with no opinion about
FastAPI at all. Look at how it's constructed:

```python
twitch_alerts = TwitchAlerts(manager.broadcast)
```

It's handed the `broadcast` **function itself** as an argument — this is **dependency injection**, and it's
just the callback idea from section 2 wearing a suit. `twitch.py` never imports FastAPI, never knows what a
WebSocket is; it only knows it has something to call when an event lands. You could swap in a function that
writes to a text file and the module wouldn't notice.

**The subscription analogy.** EventSub is a magazine subscription: you tell Twitch once *which* events you
care about and where to send them, then they arrive on their own forever.

| Step | What happens |
|---|---|
| `Twitch(client_id, client_secret)` | Identifies the *app* — says nothing about whose data it reads |
| `UserAuthenticationStorageHelper(...).bind()` | Identifies *you*. Opens a browser tab once, then caches the token in `.twitch_tokens.json` so restarts are silent |
| `get_users(logins=[channel])` | Twitch's API wants numeric ids, not names, so look yours up |
| `listen_channel_follow_v2(uid, uid, self._on_follow)` | Place the subscription, hand over the callback |

**Scopes** are the permissions slip: `MODERATOR_READ_FOLLOWERS` for follows,
`CHANNEL_READ_SUBSCRIPTIONS` for subs, `BITS_READ` for cheers. Raids need none — a raid is public.

Two API quirks the comments call out, both easy to get wrong:

- `channel.follow` is v2-only and wants a *moderator* id as well as a broadcaster id. You're your own
  moderator, so `uid` goes in twice.
- `listen_channel_raid` takes the **callback first**, unlike the other three. No reason; it just does.

Then each handler flattens Twitch's payload into the same little dict the control panel sends:

```python
def _alert(kind: str, user: str, text: str, amount: int | None = None) -> dict:
    return {"type": "alert", "kind": kind, "user": user, "text": text, "amount": amount}
```

**This is the payoff of Phase 4.** A real follow and a button press produce the *same shape of message* on
the *same relay*, so the overlay cannot tell them apart — and needed no changes to support Twitch at all.
`kind`, `user` and `amount` were *added* fields, never renamed ones, so older overlay code kept working
while new code got more to style with. Additive change is cheap; renames are not.

**Testing without a live stream.** The Twitch CLI ships a mock server, and `.env` can point EventSub at it:

```sh
twitch event websocket start-server -S -p 8081
```

On startup, when it detects the mock, `start()` prints ready-to-paste trigger commands. That's not a
nicety — twitchAPI matches an incoming event by the subscription id it got back when subscribing, and the
mock's `trigger` invents a **random** id unless you pass `-u`. A bare trigger is silently dropped and you
sit there wondering why nothing fired.

---

## 5. The frontend — Vue

### What a component is

A `.vue` file is one self-contained piece of UI with three sections:

| Block | Holds | Analogy |
|---|---|---|
| `<script setup lang="ts">` | The logic and data | The brain |
| `<template>` | The HTML structure | The body |
| `<style scoped>` | The looks | The clothes |

### Reactivity — the whiteboard

```ts
const alertText = ref('New Follower: Innoruuk')
```

A **ref** is a *reactive* container. Think of a **whiteboard in a room full of people**: write a new value
and everyone watching instantly sees it — you never have to tap each person on the shoulder.

That's the leap from plain HTML/JS. You don't tell the page to update; you change the value and the page
follows.

**The one quirk:** in `<script>` you go through `.value`, but in `<template>` you write the bare name.

```ts
alertText.value = 'ok'          // in script — need .value
```
```html
<p>{{ alertText }}</p>          <!-- in template — Vue unwraps it -->
```

There are three reactive tools, and picking the right one is most of the skill:

| Tool | Means | Example in this repo |
|---|---|---|
| `ref` | A value I set myself | `const apiOk = ref<boolean \| null>(null)` |
| `computed` | A value *derived* from others; recalculates itself | `const kindNames = computed(() => Object.keys(store.config?.alerts.kinds ?? {}))` |
| `watch` | *Do something* when a value changes | the alert handler in `AlertOverlay.vue` |

> **The rule of thumb:** if you can *calculate* it from something you already have, it's a `computed` —
> never a `ref` you remember to update by hand. `kindNames` can't drift out of sync with the config,
> because it isn't stored at all; it's re-derived on demand.

### Directives — attributes that *do* things

Regular HTML attributes are labels. Vue's **directives** are instructions:

| Directive | Meaning | Your code |
|---|---|---|
| `@click` | Run this when clicked | `@click="sendAlert(name)"` |
| `v-model` | Two-way bind an input to a ref | `<input v-model="obsScene" />` |
| `v-if` | Only render when true | `<section class="card wide" v-if="draft">` |
| `v-for` | Repeat this element per item | `<button v-for="name in kindNames" :key="name">` |
| `:prop` | Bind an attribute to a *value* rather than literal text | `:style="{ borderColor: kind.accent }"` |
| `{{ }}` | Print a value here | `{{ obsStatus }}` |

`v-model` is worth dwelling on: it's a **two-way** link. Type in the box and `obsScene` updates; change
`obsScene` in code and the box updates. One line replaces a pile of event handlers.

`v-for` always wants a `:key` — a stable, unique id per row. It's how Vue tells "the list changed" from
"one row's text changed" and avoids re-rendering everything. That's the entire reason the store stamps an
`id` onto logged events: two identical alerts would otherwise be indistinguishable.

### The store — `stores/overlay.ts`

**The shared utility closet.** In an apartment building you don't give every unit its own water heater —
there's one in a shared closet everyone taps. The Pinia **store** is that closet.

`ControlView` and `AlertOverlay` both call `useOverlayStore()` and get the **same** instance — the same
WebSocket, the same `connected` flag. That's the whole point: the control panel sends on the same pipe the
overlay listens to.

Inside, the store wires up four **callbacks**:

```ts
socket.onopen  = () => (connected.value = true)              // line came up
socket.onclose = () => { connected.value = false; setTimeout(connect, 2000) }  // dropped → redial in 2s
socket.onerror = () => socket?.close()                       // funnel errors into the redial
socket.onmessage = (e) => { ... }                            // mail arrived
```

You don't call these; the browser does. Like leaving a phone number: "call me when it arrives."
`onclose` retrying every 2 seconds is why your overlay survives a backend restart mid-stream — and
`onerror` closing the socket deliberately is what funnels *errors* into that same retry, since an error
doesn't always fire `onclose` by itself.

`onmessage` now does three jobs, and the order matters:

```ts
const message = JSON.parse(e.data)
if (message.type === 'config') {
  config.value = message.config
  return
}
lastEvent.value = message
recentEvents.value.unshift({ ...message, at: Date.now(), id: ++eventSeq })
if (recentEvents.value.length > MAX_LOG) recentEvents.value.pop()
```

- **Config pushes ride the same wire as alerts**, so they're intercepted first and routed to `config`.
  Without that early `return`, saving a colour would land in `lastEvent` and the overlay would try to
  display your settings as an alert.
- `{ ...message, at, id }` is the **spread**: copy every property of `message`, then add two more. A new
  object, not a mutation of the original.
- `unshift` puts it at the front (newest first, so the template needs no reversing) and `pop` drops the
  oldest past 50. A hand-rolled ring buffer — without it a long stream grows that array forever.

### The overlay — `views/AlertOverlay.vue`

```ts
watch(() => store.lastEvent, (event) => {
  if (event?.type !== 'alert') return
  alertText.value = event.text ?? ''
  alertKind.value = event.kind ?? ''
  alertShowing.value = true
  playSound(kindConfig.value.sound, store.config?.alerts.volume ?? FALLBACK_VOLUME)
  clearTimeout(hideTimer)
  hideTimer = setTimeout(() => {
    alertShowing.value = false
  }, store.config?.alerts.duration_ms ?? FALLBACK_DURATION_MS)
})
```

- `watch` = "run this whenever that value changes." A motion sensor on the whiteboard.
- `event?.type` — the `?.` is **optional chaining**: "if `event` is null, don't crash, just give null."
  Necessary because `lastEvent` starts as `null`.
- `??` is the **nullish coalescing** operator: "use the left side unless it's null/undefined." It's how
  every config read here gets a fallback, so an alert firing before `/api/config` answers still looks
  right instead of unstyled.
- `clearTimeout(hideTimer)` cancels the previous hide. Without it, two alerts in a row means the first
  one's timer fires and hides the *second* one early.

The styling is driven entirely by config, through one small conversion:

```ts
const accent = computed(() => {
  const n = parseInt(kindConfig.value.accent.slice(1), 16)
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`
})
```

Config stores `#b06bff`, but the CSS wants bare channels so it can build *both* a solid colour and
translucent glows from one variable:

```css
border: 4px solid rgb(var(--accent));
box-shadow: inset 0 0 33px rgb(var(--accent) / 0.18), 0 0 44px rgb(var(--accent) / 0.45);
```

`slice(1)` drops the `#`; `parseInt(..., 16)` reads hex; `>> 16 & 255` pulls out the red byte, and so on.
One config value, three visual effects, no extra fields.

### Scoped styles — the wristband

`<style scoped>` stamps every element the component renders with an attribute like `data-v-7a3b`, then
silently rewrites your CSS to require it. `main { }` becomes `main[data-v-7a3b] { }`.

**The wristband analogy:** the rule only applies to people wearing this party's wristband.

This is why `body { }` in a scoped block **does nothing** — `<body>` is outside the component, so it never
gets a wristband. `AlertOverlay` gets around it with `:global(body) { background: transparent; }`, the
explicit escape hatch. And it's exactly why a colour on `body` in the *global* stylesheet leaked onto your
transparent overlays.

### Sound — `audio/sounds.ts`

There are no audio files in this project. Every jingle is **synthesised in the browser** by the Web Audio
API, which means a "sound" is just data:

```ts
const LEVELUP: Jingle = { notes: [C5, E5, G5, C6], step: 0.075, tail: 0.34, sparkle: true }
```

Notes in order, seconds between them, how long the last one rings, and whether to double that last note an
octave up (`sparkle` — it makes the phrase land as an *arrival* rather than one more blip). One engine,
five tunes, nothing to ship.

Each note is one oscillator plus a gain envelope:

```ts
gain.gain.setValueAtTime(0, at)
gain.gain.linearRampToValueAtTime(vol, at + 0.008)
```

That 8-millisecond ramp exists because a *step* change in gain clicks audibly. Same reason the whole
jingle is scheduled at `currentTime + 0.02` — a hair in the future, so the audio thread isn't already
mid-buffer when the first note is meant to start.

The one browser rule to know:

```ts
export function primeAudio() {
  const resume = () => void context().resume()
  document.addEventListener('pointerdown', resume, { once: true })
}
```

Browsers start an `AudioContext` **suspended** until the page has seen a user gesture — otherwise every ad
on the internet would autoplay. OBS's browser source is exempt (its embedded Chromium runs with that policy
off), but a normal tab at `/overlay/alert` isn't. So: resume on the first click. If you're testing in a
plain tab and hear nothing, click the page once.

---

## 6. The three WebSockets

The single most confusing thing in the project, so: **your backend plays both roles.**

**The switchboard operator analogy.** Calls come *in* from browsers, and the operator places calls *out* —
to OBS, and to Twitch.

| | `/ws` — the relay | `OBSController` | `TwitchAlerts` |
|---|---|---|---|
| Your backend is the… | **server** (receives calls) | **client** (places calls) | **client** (places calls) |
| Who dials whom | browser → backend | backend → OBS `:4455` | backend → Twitch |
| Carries | your alert messages | OBS commands | incoming stream events |
| Library | FastAPI `WebSocket` | `simpleobsws` | `twitchAPI` EventSub |
| Who starts the conversation | either side, any time | you (request → response) | Twitch (they push) |

**Why WebSockets at all?** Normal HTTP is *mailing a letter*: you ask, you get one reply, done. To find out
about new alerts you'd have to re-ask every second. A WebSocket is *leaving the phone line open* — either
side can speak at any moment. For alerts that must appear the instant they happen, that's the difference
between instant and laggy.

Notice the shape this gives the whole app: **two inbound-to-the-backend sources of truth (you, and Twitch)
funnel into one relay, which fans out to every overlay.** Adding a fifth alert source later means writing
something that calls `manager.broadcast` — and nothing else in the project changes.

---

## 7. The config round trip

Follow one colour change all the way around, because it touches every layer:

1. **Defaults live in Python.** `DEFAULT_KINDS` in `main.py` is the source of truth. A fresh clone with no
   `config.json` runs perfectly.
2. **On boot**, `load_config()` reads the file if it exists and validates it; anything broken falls back to
   those defaults with a printed warning.
3. **The dashboard loads it** over plain HTTP — `store.loadConfig()` → `GET /api/config`.
4. **You edit a *copy*.** `draft.value = structuredClone(toRaw(store.config))`. This is the important bit:

   ```ts
   const draft = ref<AppConfig | null>(null)
   ```

   `structuredClone` makes a genuinely independent deep copy (`toRaw` first strips Vue's reactive wrapper,
   which `structuredClone` can't clone). Every control in the settings card is bound to the *draft*, so
   half-typing a hex colour doesn't strobe the live overlay. **Revert** simply clones the store's copy
   again, throwing your edits away with no round trip.
5. **Save** does `PUT /api/config`. FastAPI validates the body against `Config` *before* your code runs —
   a bad colour is rejected here, not stored.
6. **The backend writes the file and broadcasts:**

   ```python
   config = new
   save_config(config)
   await manager.broadcast({"type": "config", "config": config.model_dump()})
   ```
7. **Every open overlay updates in place**, because `onmessage` routes `type: 'config'` into the store's
   `config`, and every colour in `AlertOverlay` is a `computed` reading from it.

Step 7 is the one to appreciate: **you can retune alert colours mid-stream and the OBS browser source
never reloads.** That's reactivity and the relay paying off together.

---

## 8. The dashboard — `views/ControlView.vue`

The control panel is four cards in one grid, and most of what it does is arrangement rather than logic.
Still, three things in it are worth reading closely.

**Status that polls.**

```ts
const apiOk = ref<boolean | null>(null)
healthTimer = setInterval(checkHealth, 10_000)
onUnmounted(() => clearInterval(healthTimer))
```

`null` is a deliberate *third* state: "haven't looked yet." A light that says **down** before it has
checked is worse than one that says nothing. And the interval must be cleared in `onUnmounted` — timers
outlive the component that started them, so without that line you'd keep polling forever after navigating
away.

**Buttons derived from data.**

```ts
const kindNames = computed(() => Object.keys(store.config?.alerts.kinds ?? {}))
```
```html
<button v-for="name in kindNames" :key="name" @click="sendAlert(name)">{{ name }}</button>
```

Add a kind to `config.json` and its test button appears with no code change. The alternative — five
hardcoded buttons — is five things to forget to update.

**A grid with no breakpoints.**

```css
grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
```

Read it as a sentence: *fit as many columns as you can, each at least 340px, then let them share the extra
space equally.* On a wide monitor that's three columns; on a narrow window it becomes one, with no media
queries at all. `grid-column: 1 / -1` on the wide cards means "first grid line to last" — i.e. full width,
whatever the current column count happens to be.

---

## 9. Two modes: developing vs streaming

| | Development | Streaming |
|---|---|---|
| Command | `fastapi dev main.py` + `npm run dev` | `./stream.sh` |
| Processes | two | one |
| Frontend served by | Vite `:5173` | FastAPI `:8000`, from `frontend/dist/` |
| OBS browser source URL | `http://localhost:5173/overlay/alert` | `http://localhost:8000/overlay/alert` |
| Code changes | hot-reload instantly | need a rebuild |

They can't both run — both want port 8000.

```sh
./stream.sh              # npm run build, then `fastapi run` on :8000
./stream.sh --skip-build # skip the rebuild when the frontend hasn't changed
```

`stream.sh` rebuilds **by default**, and the reason is the nastiest failure mode in this project: a stale
`frontend/dist` fails *silently*. The mount happily serves last week's build, the overlay looks exactly
like your changes never landed, and there is no error anywhere to tell you why. Rebuilding every launch is
cheap insurance.

The script also uses `fastapi run` (no reloader, no file watcher) and `exec`s it, so Ctrl-C stops the
server directly instead of leaving it orphaned behind the shell script.

**The other cache to know about:** OBS's browser sources cache aggressively too. In the source's properties,
tick **Shutdown source when not visible** and **Refresh browser when scene becomes active**. Between those
and the startup refresh from section 3.6, you should rarely have to clear a cache by hand.

---

## 10. Follow two events, end to end

### A button press → OBS

You type `Webcam` in the Source box and press **Hide**:

1. **`v-model`** has already kept `obsSource` in sync with the box → it holds `'Webcam'`.
2. **`@click`** fires `callObs('/api/obs/source', { scene: obsScene, source: obsSource, visible: false })`.
   That `{ ... }` is an **object literal** built on the spot — three **key/value pairs**.
3. `callObs` **serializes** it with `JSON.stringify` and `fetch`es it as an HTTP POST.
4. In dev, **Vite** sees `/api` and forwards it to FastAPI on :8000. On stream there's no Vite — the
   request already went to :8000.
5. FastAPI matches `@app.post("/api/obs/source")`, **deserializes** the JSON, and validates it against the
   `SourceRequest` model — if a field is missing or the wrong type, it's rejected before your code runs.
6. The endpoint asks OBS for the source's numeric id (`GetSceneItemId`), because OBS won't take a name
   here — then sends `SetSceneItemEnabled` with `false`.
7. `OBSController.call` reuses the already-open phone line to OBS. **The webcam disappears.**
8. The endpoint returns `{"ok": true, ...}` → `callObs` writes it into the `obsStatus` **ref** and flips
   `obsOk` → the whiteboard updates → the status line and the **OBS** pill both re-render.

### A real follow → your overlay

Nothing in this path involves you at all:

1. Someone clicks Follow. **Twitch** pushes a `channel.follow` event down the EventSub websocket.
2. `twitchAPI` matches it to the subscription made at startup and calls `_on_follow`.
3. `_on_follow` builds `{"type": "alert", "kind": "follow", "user": ..., "text": "New Follower: ...", "amount": None}`
   and awaits the injected `broadcast`.
4. `ConnectionManager.broadcast` **serializes** it once and sends it to every socket on the guest list —
   your OBS overlay, and your dashboard.
5. In the overlay, `onmessage` sees it isn't a config message → `lastEvent` changes → the `watch` fires.
6. `kindConfig` (a **computed**) looks `'follow'` up in the config: label `NEW FOLLOWER`, accent `#b06bff`,
   sound `follow`. `accent` converts that hex to `176 107 255` for the CSS.
7. `playSound('follow', 0.45)` schedules two oscillator notes ~20ms out. `alertShowing` flips true, Vue's
   `<Transition>` runs the `pop-in` keyframes, and a `setTimeout` for `duration_ms` is armed to hide it.
8. In the dashboard, the same message lands in `recentEvents` and a new row appears in the Activity feed,
   tagged in the same purple.

Every layer you built, in one follow.

---

## 11. Where to poke next

Good ways to make the ideas stick:

- **Break it on purpose.** Type a scene name that doesn't exist and watch the `502` land in the OBS pill.
  Stop the backend and watch the `503`, then the **WS** pill flip to `offline` and back on its own.
- **Add a config field.** Give `AlertConfig` something like `position: str = "center"`, add a control for
  it in the settings card, and read it in `AlertOverlay`. You'll touch all seven steps of section 7 —
  that's the exercise.
- **Add a jingle.** Drop a new entry in `SOUNDS` in `sounds.ts`. It appears in every kind's dropdown
  automatically, because `SOUND_NAMES` is derived from the object. Notice the backend needed no change:
  `sound` is deliberately a plain `str`, not an enum.
- **Watch the config broadcast.** Open the overlay in one tab and the dashboard in another, change an
  accent colour, hit Save, and fire a test alert — no refresh anywhere.
- **Give the feed a purpose.** It's currently write-only. Filter it by kind, or add a "replay" button that
  re-sends a logged event through `store.send`. Everything you need is already in the store.
- **Break the additive rule on purpose.** Rename `text` to `message` in `twitch.py` only, and watch the
  overlay render an empty alert. Nothing errors — nothing *can*. Both ends agreeing on names is a
  convention you maintain, not something the code enforces.
