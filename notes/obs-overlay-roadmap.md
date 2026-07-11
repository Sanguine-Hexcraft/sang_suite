# Stream Overlay Manager — Build Roadmap & Tutorial

**The project:** a localhost-only web app that manages your OBS browser sources, overlays, and widgets (alerts, tickers, chat). One Python process runs everything. OBS points its browser sources at your own server.

**The stack (already decided, for reference):**

| Layer | Tool | Job |
|---|---|---|
| Frontend framework | Vue 3 | What you write your UI in |
| Frontend tooling | Vite | Runs the dev server, builds static files |
| Routing | Vue Router | `/control` (your dashboard) vs `/overlay/...` (what OBS sees) |
| Shared state | Pinia | Holds live overlay state, fed by WebSocket |
| Backend | FastAPI (Python) | Serves the app, owns WebSockets, talks to OBS/Twitch |
| OBS control | simpleobsws | Talk to OBS over its websocket |
| Twitch events | twitchAPI | Follows, subs, chat, etc. |
| Persistence | SQLite or JSON | Save widget configs |

**The mental model to keep the whole time:**

```
┌─────────────┐     WebSocket      ┌──────────────┐
│  /control    │ ◄───────────────► │              │
│  (your       │                    │   FastAPI    │ ◄──► OBS (obs-websocket)
│   dashboard) │                    │   (Python)   │ ◄──► Twitch (twitchAPI)
└─────────────┘                    │              │
┌─────────────┐     WebSocket      │  serves the  │
│ /overlay/... │ ◄───────────────► │  built Vue   │
│ (OBS browser │                    │  files too   │
│  sources)    │                    └──────────────┘
└─────────────┘
```

The control page and the overlay pages are the *same Vue app*, just different routes. They both connect to FastAPI over WebSockets. When you click "show alert" on `/control`, FastAPI relays it to every `/overlay/...` page, and OBS displays it. That relay loop is the heart of the whole project — everything else is decoration.

---

## Phase 0 — Environment check (30 min)

You've already got nvm + npm. Verify everything:

```bash
node --version    # want 20.x or 22.x LTS
npm --version
python3 --version # want 3.11+
```

Create the project structure. One repo, two halves:

```bash
mkdir overlay-manager && cd overlay-manager
mkdir backend
# frontend/ gets created by the Vue scaffolder in Phase 1
```

**Checkpoint:** all three version commands print sensible numbers.

---

## Phase 1 — Scaffold the Vue app & understand what Vite gave you (1–2 evenings)

```bash
npm create vue@latest frontend
```

When it asks questions, say **Yes** to: TypeScript (optional — say No if it's one thing too many right now), **Vue Router**, **Pinia**. Say No to the rest (testing, ESLint etc. can come later).

```bash
cd frontend
npm install
npm run dev
```

Open the URL it prints (usually `http://localhost:5173`). That page you're looking at is Vite serving your Vue app.

### Guided tour of what's in the folder

- `index.html` — the single real HTML page. Everything mounts into `<div id="app">`.
- `src/main.js` — the entry point. Creates the Vue app, installs Router and Pinia, mounts it.
- `src/App.vue` — the root component. Contains `<RouterView />`, which is the hole that the current route's page renders into.
- `src/router/index.js` — the route table. URL → component mapping. This is where `/control` and `/overlay/alert` will live.
- `src/stores/` — Pinia stores. Shared state any component can read.
- `src/components/` — reusable pieces.
- `vite.config.js` — Vite's settings. You'll touch this exactly once, in Phase 3, to add a proxy.

### Exercise for this phase

Delete the demo content and create two routes:

`src/router/index.js`:

```js
import { createRouter, createWebHistory } from 'vue-router'
import ControlView from '../views/ControlView.vue'
import AlertOverlay from '../views/AlertOverlay.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/control', component: ControlView },
    { path: '/overlay/alert', component: AlertOverlay },
  ],
})

export default router
```

`src/views/ControlView.vue`:

```vue
<template>
  <main>
    <h1>Control Panel</h1>
    <button @click="count++">Test clicks: {{ count }}</button>
  </main>
</template>

<script setup>
import { ref } from 'vue'
const count = ref(0)
</script>
```

`src/views/AlertOverlay.vue`:

```vue
<template>
  <div class="alert">FOLLOW ALERT PLACEHOLDER</div>
</template>

<style scoped>
.alert {
  color: white;
  font-size: 3rem;
  text-shadow: 0 0 10px black;
}
body { background: transparent; }
</style>
```

**Checkpoint:** `localhost:5173/control` shows a working button; `localhost:5173/overlay/alert` shows big text. You understand that both are the same app on different routes.

---

## Phase 2 — FastAPI backend skeleton (1 evening)

```bash
cd ../backend
python3 -m venv venv
source venv/bin/activate
pip install "fastapi[standard]" simpleobsws twitchAPI
```

`backend/main.py`:

```python
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/health")
async def health():
    return {"status": "ok"}
```

Run it:

```bash
fastapi dev main.py
```

Visit `http://localhost:8000/api/health` — you should see the JSON. Also visit `http://localhost:8000/docs` — FastAPI auto-generates interactive API docs, which you'll use constantly for testing.

**Checkpoint:** health endpoint returns JSON; you've clicked around `/docs`.

---

## Phase 3 — Connect the two halves (1 evening)

Right now you have two servers: Vite on 5173, FastAPI on 8000. During development you keep both running, and tell Vite to forward API calls to FastAPI. Add to `frontend/vite.config.js`:

```js
export default defineConfig({
  // ...existing config...
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
    },
  },
})
```

Now in `ControlView.vue`, fetch from the backend:

```vue
<script setup>
import { ref, onMounted } from 'vue'
const status = ref('...')
onMounted(async () => {
  const res = await fetch('/api/health')
  status.value = (await res.json()).status
})
</script>

<template>
  <main>
    <h1>Control Panel</h1>
    <p>Backend: {{ status }}</p>
  </main>
</template>
```

**Checkpoint:** the control page displays "Backend: ok". Your frontend is now talking to Python.

---

## Phase 4 — WebSockets: the heart of the project (2–3 evenings)

This is the phase where the app becomes *the app*. Goal: click a button on `/control`, see the alert appear on `/overlay/alert` in another browser tab, instantly.

### Backend: a connection manager

Add to `main.py`:

```python
from fastapi import WebSocket, WebSocketDisconnect
import json

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.remove(ws)

    async def broadcast(self, message: dict):
        for ws in self.connections:
            await ws.send_text(json.dumps(message))

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            data = json.loads(await ws.receive_text())
            # For now: relay everything to everyone
            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect(ws)
```

### Frontend: a Pinia store that owns the socket

`src/stores/overlay.js`:

```js
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useOverlayStore = defineStore('overlay', () => {
  const connected = ref(false)
  const lastEvent = ref(null)
  let socket = null

  function connect() {
    socket = new WebSocket(`ws://${location.host}/ws`)
    socket.onopen = () => (connected.value = true)
    socket.onclose = () => {
      connected.value = false
      setTimeout(connect, 2000) // auto-reconnect
    }
    socket.onmessage = (e) => (lastEvent.value = JSON.parse(e.data))
  }

  function send(event) {
    socket?.send(JSON.stringify(event))
  }

  return { connected, lastEvent, connect, send }
})
```

Control page sends: `store.send({ type: 'alert', text: 'New follower!' })` on a button click. Overlay page watches `store.lastEvent` and shows/hides the alert (a `watch()` plus a `setTimeout` to hide after a few seconds).

**Checkpoint:** two browser tabs open — clicking the button in `/control` makes the alert flash in `/overlay/alert`. When this works, celebrate. This is the core loop of the entire product.

---

## Phase 5 — Put it in OBS (1 evening)

1. In OBS: **Add → Browser Source** → URL `http://localhost:5173/overlay/alert`, size 1920×1080.
2. Make the overlay background transparent (OBS browser sources render transparent backgrounds automatically if the page background is transparent — check your CSS).
3. Trigger the alert from `/control` in your normal browser and watch it appear in the OBS preview.

**Checkpoint:** alert fires inside OBS. You now have a working (if bare) overlay system.

---

## Phase 6 — OBS control from Python (1–2 evenings)

In OBS: **Tools → WebSocket Server Settings** → enable, note the port (4455) and password.

In the backend, use `simpleobsws` to connect and do something simple — switch scenes or toggle a source's visibility:

```python
import simpleobsws

obs = simpleobsws.WebSocketClient(
    url="ws://localhost:4455",
    password="your-obs-password",
)

async def toggle_source(scene: str, source_id: int, visible: bool):
    await obs.connect()
    await obs.wait_until_identified()
    await obs.call(simpleobsws.Request('SetSceneItemEnabled', {
        'sceneName': scene,
        'sceneItemId': source_id,
        'sceneItemEnabled': visible,
    }))
```

Expose it as an API endpoint, add a button on `/control` that calls it.

**Checkpoint:** a button in your dashboard hides/shows a source in OBS.

---

## Phase 7 — Twitch events (2–3 evenings)

Register an app at the Twitch dev console to get a client ID/secret, then use `twitchAPI`'s EventSub (websocket transport — no public URL needed, perfect for localhost) to listen for follows/subs. When an event arrives, call `manager.broadcast(...)` — the exact same relay you built in Phase 4 — and your overlay fires automatically.

This phase has the most fiddly setup (OAuth scopes, tokens), so budget patience, not just time.

**Checkpoint:** following your channel from a test account makes the alert fire in OBS with no manual clicking.

---

## Phase 8 — Persistence + production build (1–2 evenings)

**Persistence:** start with a JSON file (`config.json`) holding widget settings — alert text, colors, durations. Load on startup, save on change via an API endpoint. Move to SQLite later only if you feel the need.

**Production build (the single-process goal):**

```bash
cd frontend && npm run build   # produces frontend/dist/
```

Then in FastAPI, serve those files:

```python
from fastapi.staticfiles import StaticFiles
app.mount("/", StaticFiles(directory="../frontend/dist", html=True), name="app")
```

(Mount this *after* your API/WS routes so they take priority. You'll also want a small catch-all so `/control` and `/overlay/...` serve `index.html` — ask me when you get here.)

Now `fastapi run main.py` is the only process. Point OBS at `http://localhost:8000/overlay/alert` instead of 5173. Vite's job is done until the next time you develop.

**Checkpoint:** one Python process, no Vite running, everything still works in OBS.

---

## After that (the fun list)

- More widget types: ticker, chat display, goal bars
- Widget config UI on `/control` (this is where Pinia + persistence really pay off)
- Animations (CSS transitions or Vue's `<Transition>` component)
- Sound on alerts
- Scene-aware widgets (react to OBS scene changes via simpleobsws events)

## Rules of thumb while learning

- **One phase at a time.** Don't touch Twitch until the WebSocket relay works.
- **Keep both dev servers running** in two terminals during Phases 3–7.
- When something breaks, check the browser devtools console (F12) *and* the FastAPI terminal — errors show up in one or the other.
- Commit to git at every checkpoint so you can always roll back to "it worked."
