<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useOverlayStore } from '@/stores/overlay'

const store = useOverlayStore()
const alertShowing = ref(false)
const alertText = ref('')

watch(() => store.lastEvent, (event) => {
  // steps go here
  if (event?.type !== 'alert') return
  alertShowing.value = true
  alertText.value = event.text ?? ''
  setTimeout(() => {
    alertShowing.value = false
  }, 4000)
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
  color: white;
  font-size: 3rem;
  text-shadow: 0 0 10px black;
}

body {
  background: transparent;
}
</style>
