<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useOverlayStore } from '@/stores/overlay'

const status = ref('...')
const store = useOverlayStore()

onMounted(async () => {
  store.connect()
  const res = await fetch('/api/health')
  status.value = (await res.json()).status
})
</script>

<template>
  <main>
    <h1>Control Panel</h1>
    <p>Backend: {{ status }}</p>
    <p>WS: {{ store.connected }}</p>
    <button @click="store.send({ type: 'alert', text: 'New Follower: MikeTheMad' })">Send Alert</button>
  </main>
</template>
