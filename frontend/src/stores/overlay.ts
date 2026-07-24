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

export const useOverlayStore = defineStore('overlay', () => {
  const connected = ref(false)
  const lastEvent = ref<OverlayEvent | null>(null)
  let socket: WebSocket | null = null

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
    socket.onmessage = (e) => (lastEvent.value = JSON.parse(e.data))
  }

  function send(event: OverlayEvent) {
    socket?.send(JSON.stringify(event))
  }

  return { connected, lastEvent, connect, send }
})
