<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useOverlayStore } from '@/stores/overlay'

const store = useOverlayStore()
const alertShowing = ref(false)
const alertText = ref('')
let hideTimer: ReturnType<typeof setTimeout> | undefined

watch(() => store.lastEvent, (event) => {
  // steps go here
  if (event?.type !== 'alert') return
  alertShowing.value = true
  alertText.value = event.text ?? ''
  // Cancel any in-flight hide so a new alert gets its full 4s
  // instead of being cut short by the previous alert's timer.
  clearTimeout(hideTimer)
  hideTimer = setTimeout(() => {
    alertShowing.value = false
  }, 8000)
})

onMounted(() => {
  store.connect()
})
</script>



<template>
  <div v-if="alertShowing" class="alert">{{ alertText }}</div>
</template>


<style scoped>
.alert {
  color: red;
  font-size: 8rem;
  text-shadow: 0 0 10px black;
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
}

:global(body) {
  background: transparent;
}
</style>
