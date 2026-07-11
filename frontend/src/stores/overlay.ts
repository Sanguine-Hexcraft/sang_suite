import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface OverlayEvent {
  type: string
  text?: string
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
    socket.onmessage = (e) => (lastEvent.value = JSON.parse(e.data))
  }

  function send(event: OverlayEvent) {
    socket?.send(JSON.stringify(event))
  }

  return { connected, lastEvent, connect, send }
})
