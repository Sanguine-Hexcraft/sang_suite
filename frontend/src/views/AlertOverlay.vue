<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useOverlayStore, type AlertKindConfig } from '@/stores/overlay'
import { playSound, primeAudio } from '@/audio/sounds'

const store = useOverlayStore()
const alertShowing = ref(false)
const alertText = ref('')
const alertKind = ref('')
let hideTimer: ReturnType<typeof setTimeout> | undefined

// Used only in the gap before /api/config answers, or if it fails outright —
// an alert firing in that window should still be legible rather than unstyled.
const FALLBACK: AlertKindConfig = { label: 'ALERT', accent: '#ff2d55', sound: 'levelup' }
const FALLBACK_DURATION_MS = 8000
const FALLBACK_VOLUME = 0.45

// Manual alerts from the control panel arrive with no `kind`, so they fall
// through to the 'generic' entry.
const kindConfig = computed<AlertKindConfig>(
  () => store.config?.alerts.kinds[alertKind.value || 'generic'] ?? FALLBACK,
)
const headline = computed(() => kindConfig.value.label)

// The CSS composes glows as rgb(var(--accent) / alpha), which needs the
// channels bare rather than as a hex literal.
const accent = computed(() => {
  const n = parseInt(kindConfig.value.accent.slice(1), 16)
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`
})

watch(() => store.lastEvent, (event) => {
  if (event?.type !== 'alert') return
  alertText.value = event.text ?? ''
  alertKind.value = event.kind ?? ''
  alertShowing.value = true
  // Reads through the computed, so it picks up the kind assigned just above.
  playSound(kindConfig.value.sound, store.config?.alerts.volume ?? FALLBACK_VOLUME)
  // Cancel any in-flight hide so a new alert gets its full duration
  // instead of being cut short by the previous alert's timer.
  clearTimeout(hideTimer)
  hideTimer = setTimeout(() => {
    alertShowing.value = false
  }, store.config?.alerts.duration_ms ?? FALLBACK_DURATION_MS)
})

onMounted(() => {
  store.connect()
  store.loadConfig()
  primeAudio()
})
</script>


<template>
  <Transition name="pop">
    <div v-if="alertShowing" class="alert">
      <div class="card" :style="{ '--accent': accent }">
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

/* --accent is set inline from config as an "r g b" triple, so it can drive
   both solid and alpha colours. The value here is only a safety net. */
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
