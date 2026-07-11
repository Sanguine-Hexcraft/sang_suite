<script setup lang="ts">
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
    <p> Backend: {{ status }}</p>
  </main>
</template>
