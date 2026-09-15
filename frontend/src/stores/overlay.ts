import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface OverlayEvent {
  type: string
  text?: string
  // Phase 9: a monotonic id stamped by the server on every broadcast. Used to
  // ask for the gap after a reconnect; config pushes deliberately carry none.
  id?: number
  // Added in Phase 7. The overlay only needs type/text to render, but Twitch
  // alerts also carry these so views can style per-event later without a change
  // to the backend. kind = follow | sub | cheer | raid; amount = bits / raid
  // viewers / sub tier, or null when the event has no number.
  kind?: string
  user?: string
  amount?: number | null
}

// Widget settings, mirroring the Pydantic models in backend/main.py. Fetched
// over HTTP on load, then kept fresh by `type: 'config'` relay messages.
export interface AlertKindConfig {
  label: string
  accent: string // "#rrggbb"
  sound: string // a key of SOUNDS in @/audio/sounds
}

export interface AppConfig {
  alerts: {
    duration_ms: number
    volume: number // 0–1, peak gain of the alert jingle
    kinds: Record<string, AlertKindConfig>
  }
}

// An event plus the bookkeeping the activity feed needs: `at` for the
// timestamp column, `id` because two identical alerts would otherwise be
// indistinguishable to v-for's :key.
export interface LoggedEvent extends OverlayEvent {
  at: number
  id: number
}

// Long enough to scroll back through a raid, short enough that a stream-length
// session doesn't grow the array forever.
const MAX_LOG = 50

// Module scope, NOT localStorage, and deliberately not inside the store.
// The case worth rescuing is the reconnect loop -- onclose -> setTimeout ->
// connect() inside a page that never unloaded, which is what a backend restart
// or a network blip looks like. This survives that, so the replay fires.
// A full page reload wipes it, last_id is 0, and nothing replays: an OBS
// browser source starting fresh mid-stream should not dump backlog on canvas.
let lastSeenId = 0

export const useOverlayStore = defineStore('overlay', () => {
  const connected = ref(false)
  const lastEvent = ref<OverlayEvent | null>(null)
  const recentEvents = ref<LoggedEvent[]>([])
  const config = ref<AppConfig | null>(null)
  let socket: WebSocket | null = null
  let eventSeq = 0

  async function loadConfig() {
    const res = await fetch('/api/config')
    config.value = await res.json()
  }

  function connect() {
    // Already open or opening? Don't stack a second socket.
    if (socket && socket.readyState !== WebSocket.CLOSED) return

    socket = new WebSocket(`ws://${location.host}/ws`)
    socket.onopen = () => {
      connected.value = true
      // Ask for anything broadcast while we were away. 0 on a fresh page load
      // means "send nothing".
      socket?.send(JSON.stringify({ type: 'hello', last_id: lastSeenId }))
    }
    socket.onclose = () => {
      connected.value = false
      setTimeout(connect, 2000) // auto-reconnect
    }
    // An error doesn't always fire onclose on its own, so close explicitly
    // to funnel it into the same reconnect loop above.
    socket.onerror = () => socket?.close()
    socket.onmessage = (e) => {
      const message = JSON.parse(e.data)
      // Config pushes ride the same relay as alerts; keep them out of
      // lastEvent so a settings save doesn't look like an overlay event.
      if (message.type === 'config') {
        config.value = message.config
        return
      }
      // Track before dispatch so a replayed batch advances the watermark even
      // if a later handler throws.
      if (typeof message.id === 'number' && message.id > lastSeenId) {
        lastSeenId = message.id
      }
      lastEvent.value = message
      // Newest first, so the feed reads top-down without reversing in the
      // template. The relay echoes to every client including the sender, so
      // this logs manual and Twitch alerts alike.
      recentEvents.value.unshift({ ...message, at: Date.now(), id: ++eventSeq })
      if (recentEvents.value.length > MAX_LOG) recentEvents.value.pop()
    }
  }

  function send(event: OverlayEvent) {
    socket?.send(JSON.stringify(event))
  }

  return { connected, lastEvent, recentEvents, config, connect, loadConfig, send }
})
