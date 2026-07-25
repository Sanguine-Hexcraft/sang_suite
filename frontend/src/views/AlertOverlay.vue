<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useOverlayStore } from '@/stores/overlay'

const store = useOverlayStore()
const alertShowing = ref(false)
const alertText = ref('')
const alertKind = ref('')
let hideTimer: ReturnType<typeof setTimeout> | undefined

// Banner above the message. Manual alerts from the control panel arrive with
// no `kind`, so they fall through to the generic label.
const HEADLINES: Record<string, string> = {
  follow: 'NEW FOLLOWER',
  sub: 'NEW SUBSCRIBER',
  cheer: 'BITS INCOMING',
  raid: 'INCOMING RAID',
}
const headline = computed(() => HEADLINES[alertKind.value] ?? 'ALERT')

watch(() => store.lastEvent, (event) => {
  if (event?.type !== 'alert') return
  alertText.value = event.text ?? ''
  alertKind.value = event.kind ?? ''
  alertShowing.value = true
  // Cancel any in-flight hide so a new alert gets its full 8s
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
  <Transition name="pop">
    <div v-if="alertShowing" class="alert" :class="`kind-${alertKind || 'generic'}`">
      <div class="card">
        <p class="headline">{{ headline }}</p>
        <p class="message">{{ alertText }}</p>
      </div>
    </div>
  </Transition>
</template>


<style scoped>
/* local() picks up the system install; the url() is the fallback for when it
   isn't visible (e.g. Flatpak OBS can't see /usr/local/share/fonts). Drop the
   src line entirely if you don't bundle the file. */
@font-face {
  font-family: 'Departure Mono';
  src: local('Departure Mono'), url('/fonts/DepartureMono-Regular.otf') format('opentype');
  font-display: block;
}

.alert {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  font-family: 'Departure Mono', ui-monospace, monospace;
  /* Departure Mono is a pixel font. Let the browser antialias it and the
     stair-steps smear; turning smoothing off keeps the grid crisp. */
  -webkit-font-smoothing: none;
  font-smooth: never;
  pointer-events: none;
}

/* Accent as an "r g b" triple so it can drive both solid and alpha colours. */
.card {
  --accent: 255 45 85;
  padding: 33px 55px;
  text-align: center;
  background: rgb(9 7 12 / 0.85);
  border: 4px solid rgb(var(--accent));
  box-shadow:
    inset 0 0 33px rgb(var(--accent) / 0.18),
    0 0 44px rgb(var(--accent) / 0.45);
}

.kind-follow .card { --accent: 176 107 255; }
.kind-sub    .card { --accent: 255 209 102; }
.kind-cheer  .card { --accent: 77 214 255; }
.kind-raid   .card { --accent: 255 122 0; }
/* kind-generic keeps the crimson default. */

.headline {
  margin: 0 0 11px;
  font-size: 22px;
  letter-spacing: 0.5em;
  /* Letter-spacing adds a trailing gap after the last glyph; nudge back to centre. */
  text-indent: 0.5em;
  color: rgb(var(--accent));
  text-shadow: 0 0 12px rgb(var(--accent) / 0.9);
}

.message {
  margin: 0;
  font-size: 55px;
  line-height: 1.2;
  color: #fff;
  text-shadow:
    0 3px 0 rgb(0 0 0 / 0.9),
    0 0 22px rgb(var(--accent) / 0.55);
}

.pop-enter-active { animation: pop-in 320ms cubic-bezier(0.2, 1.4, 0.4, 1); }
.pop-leave-active { animation: pop-in 260ms ease-in reverse; }

@keyframes pop-in {
  from { opacity: 0; transform: translateY(28px) scale(0.94); }
  to   { opacity: 1; transform: none; }
}

:global(body) {
  background: transparent;
}
</style>
