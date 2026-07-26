import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface OverlayEvent {
  type: string
  text?: string
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
}

export interface AppConfig {
  alerts: {
    duration_ms: number
    volume: number // 0–1, peak gain of the alert jingle
    kinds: Record<string, AlertKindConfig>
  }
}

export const useOverlayStore = defineStore('overlay', () => {
  const connected = ref(false)
  const lastEvent = ref<OverlayEvent | null>(null)
  const config = ref<AppConfig | null>(null)
  let socket: WebSocket | null = null

  async function loadConfig() {
    const res = await fetch('/api/config')
    config.value = await res.json()
  }

  function connect() {
    // Already open or opening? Don't stack a second socket.
    if (socket && socket.readyState !== WebSocket.CLOSED) return

    socket = new WebSocket(`ws://${location.host}/ws`)
    socket.onopen = () => (connected.value = true)
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
      if (message.type === 'config') config.value = message.config
      else lastEvent.value = message
    }
  }

  function send(event: OverlayEvent) {
    socket?.send(JSON.stringify(event))
  }

  return { connected, lastEvent, config, connect, loadConfig, send }
})
