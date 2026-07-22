# How sang_suite Works

A plain-language tour of the whole project, with the vocabulary spelled out. Every example is real code
from this repo, so you can open the file and follow along.

---

## 1. The 10,000-foot view

**The restaurant analogy.** Your app is a restaurant:

- The **frontend** (Vue) is the *dining room* — everything the customer sees and touches.
- The **backend** (FastAPI) is the *kitchen* — does the real work, out of sight.
- **Vite** is the *waiter* — the dining room never walks into the kitchen. It hands orders to the waiter,
  who carries them back.

That last part is why **both servers must be running**. Your browser only ever talks to Vite (port 5173).
When it asks for something starting with `/api` or `/ws`, Vite quietly forwards it to FastAPI (port 8000)
and brings the answer back. That forwarding rule lives in `frontend/vite.config.ts`:

```ts
proxy: {
  '/api': 'http://localhost:8000',
  '/ws': { target: 'ws://localhost:8000', ws: true },
}
```

If the kitchen is closed (backend not running), the waiter comes back with `ECONNREFUSED` — that's the
error you saw spamming the Vite console.

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

> **Blueprint vs building.** `class OBSController` is the blueprint — it describes what an OBS controller
> *would* have. `obs = OBSController(...)` constructs the actual building. You can make many buildings from
> one blueprint; here you only need one.

### Async

| Term | What it means |
|---|---|
| **`async def`** | This function does slow things (network, disk) and can be paused. |
| **`await`** | "Pause here until this finishes — and let other work run meanwhile." |

> **The waiter analogy again.** A waiter who `await`s doesn't stand frozen at the kitchen door while your
> steak cooks. They go take three other tables' orders and come back when it's ready. That's why your
> backend can serve many overlay pages at once without freezing.

---

## 3. The backend — `backend/main.py`

One file, three jobs.

### Job 1: Answer HTTP requests

```python
@app.get("/api/health")
async def health():
    return {"status": "ok"}
```

- `@app.get("/api/health")` is a **decorator** — a *sign hung on a door*. It tells FastAPI: "when a GET
  request arrives for this path, run the function below."
- The function returns a Python **dict**; FastAPI **serializes** it to JSON automatically.
- Every path starts with `/api` so the Vite waiter knows to forward it. A route without that prefix would
  never be found by the browser.

### Job 2: Relay alerts (`ConnectionManager`)

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

### Job 3: Control OBS (`OBSController`)

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

---

## 4. The frontend — Vue

### What a component is

A `.vue` file is one self-contained piece of UI with three sections:

| Block | Holds | Analogy |
|---|---|---|
| `<script setup lang="ts">` | The logic and data | The brain |
| `<template>` | The HTML structure | The body |
| `<style scoped>` | The looks | The clothes |

### Reactivity — the whiteboard

```ts
const status = ref('...')
```

A **ref** is a *reactive* container. Think of a **whiteboard in a room full of people**: write a new value
and everyone watching instantly sees it — you never have to tap each person on the shoulder.

That's the leap from plain HTML/JS. You don't tell the page to update; you change the value and the page
follows.

**The one quirk:** in `<script>` you go through `.value`, but in `<template>` you write the bare name.

```ts
status.value = 'ok'      // in script — need .value
```
```html
<p>Backend: {{ status }}</p>   <!-- in template — Vue unwraps it -->
```

### Directives — attributes that *do* things

Regular HTML attributes are labels. Vue's **directives** are instructions:

| Directive | Meaning | Your code |
|---|---|---|
| `@click` | Run this when clicked | `@click="callObs(...)"` |
| `v-model` | Two-way bind an input to a ref | `<input v-model="obsScene" />` |
| `v-if` | Only render when true | `<div v-if="alertShowing">` |
| `{{ }}` | Print a value here | `{{ obsStatus }}` |

`v-model` is worth dwelling on: it's a **two-way** link. Type in the box and `obsScene` updates; change
`obsScene` in code and the box updates. One line replaces a pile of event handlers.

### The store — `stores/overlay.ts`

**The shared utility closet.** In an apartment building you don't give every unit its own water heater —
there's one in a shared closet everyone taps. The Pinia **store** is that closet.

`ControlView` and `AlertOverlay` both call `useOverlayStore()` and get the **same** instance — the same
WebSocket, the same `connected` flag. That's the whole point: the control panel sends on the same pipe the
overlay listens to.

Inside, the store wires up four event handlers:

```ts
socket.onopen    = () => (connected.value = true)   // line came up
socket.onclose   = () => { ...; setTimeout(connect, 2000) }  // dropped → redial in 2s
socket.onerror   = () => socket?.close()            // funnel errors into the redial
socket.onmessage = (e) => (lastEvent.value = JSON.parse(e.data))  // mail arrived
```

These are **callbacks** — functions you hand over to be run *later*, when something happens. You don't call
them; the browser does. Like leaving a phone number: "call me when it arrives."

`onclose` retrying every 2 seconds is why your overlay survives a backend restart mid-stream.

### Watching for alerts — `AlertOverlay.vue`

```ts
watch(() => store.lastEvent, (event) => {
  if (event?.type !== 'alert') return
  alertShowing.value = true
  clearTimeout(hideTimer)
  hideTimer = setTimeout(() => { alertShowing.value = false }, 4000)
})
```

- `watch` = "run this whenever that value changes." A motion sensor on the whiteboard.
- `event?.type` — the `?.` is **optional chaining**: "if `event` is null, don't crash, just give null."
  Necessary because `lastEvent` starts as `null`.
- `clearTimeout(hideTimer)` cancels the previous hide. Without it, two alerts in a row means the first
  one's 4-second timer fires and hides the *second* one early.

### Scoped styles — the wristband

`<style scoped>` stamps every element the component renders with an attribute like `data-v-7a3b`, then
silently rewrites your CSS to require it. `main { }` becomes `main[data-v-7a3b] { }`.

**The wristband analogy:** the rule only applies to people wearing this party's wristband.

This is why `body { }` in a scoped block **does nothing** — `<body>` is outside the component, so it never
gets a wristband. And it's exactly why a color on `body` in the *global* stylesheet leaked onto your
transparent overlays.

---

## 5. The two WebSockets

The single most confusing thing in the project, so: **your backend plays both roles.**

**The switchboard operator analogy.** Calls come *in* from browsers, and the operator places calls *out*
to OBS.

| | `/ws` — the relay | `OBSController` |
|---|---|---|
| Your backend is the… | **server** (receives calls) | **client** (places calls) |
| Who dials whom | browser → backend | backend → OBS `:4455` |
| Carries | your alert messages | OBS commands |
| Library | FastAPI `WebSocket` | `simpleobsws` |

**Why WebSockets at all?** Normal HTTP is *mailing a letter*: you ask, you get one reply, done. To find
out about new alerts you'd have to re-ask every second. A WebSocket is *leaving the phone line open* —
either side can speak at any moment. For alerts that must appear the instant they happen, that's the
difference between instant and laggy.

---

## 6. Follow one click, end to end

You type `Webcam` in the Source box and press **Hide Source**:

1. **`v-model`** has already kept `obsSource` in sync with the box → it holds `'Webcam'`.
2. **`@click`** fires `callObs('/api/obs/source', { scene: obsScene, source: obsSource, visible: false })`.
   That `{ ... }` is an **object literal** built on the spot — three **key/value pairs**.
3. `callObs` **serializes** it with `JSON.stringify` and `fetch`es it as an HTTP POST.
4. **Vite** sees `/api` and forwards it to FastAPI on :8000.
5. FastAPI matches `@app.post("/api/obs/source")`, **deserializes** the JSON, and validates it against the
   `SourceRequest` model — if a field is missing or the wrong type, it rejects it before your code runs.
6. The endpoint asks OBS for the source's numeric id (`GetSceneItemId`), because OBS won't take a name
   here — then sends `SetSceneItemEnabled` with `false`.
7. `OBSController.call` reuses the already-open phone line to OBS. **The webcam disappears.**
8. The endpoint returns `{"ok": true, ...}` → back through Vite → `callObs` writes it into the `obsStatus`
   **ref** → the whiteboard updates → `{{ obsStatus }}` re-renders. You read "ok".

Every layer you built, in one press.

---

## 7. Where to poke next

Good ways to make the ideas stick:

- **Change the alert duration.** `4000` in `AlertOverlay.vue` is milliseconds. Make it 10 seconds.
- **Break it on purpose.** Type a scene name that doesn't exist and watch the `502` come back. Stop the
  backend and watch the `503`.
- **Add a property.** Put `color` into the alert object in `ControlView.vue`, then read `event.color` in
  `AlertOverlay.vue` to style the text. This teaches the key lesson: *both ends must agree on the name.*
  Nothing enforces it but you.
- **Watch the reconnect.** Open the overlay, stop the backend, watch `WS: false`, restart it, watch it come
  back on its own within 2 seconds.
