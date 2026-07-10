<template>
  <main>
    <h1>Control Panel</h1>
    <button @click="count++">Test clicks: {{ count }}</button>
  </main>
</template>

<script setup lang="ts">
import { ref } from 'vue'
const count = ref(0)
</script>
