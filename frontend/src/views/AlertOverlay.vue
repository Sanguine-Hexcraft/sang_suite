<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useOverlayStore, type AlertKindConfig, type OverlayEvent } from '@/stores/overlay'
import { playSound, primeAudio } from '@/audio/sounds'

const store = useOverlayStore()
const alertShowing = ref(false)
const alertText = ref('')
const alertKind = ref('')

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

// --- alert queue ------------------------------------------------------------
// Alerts arrive whenever Twitch feels like it — two follows a second apart are
// normal, and a raid can land dozens at once. Showing them as they arrive means
// each one overwrites the last mid-display, so nobody's alert is actually seen.
// Instead they wait in line and are shown one at a time, each for its full
// duration. Plain arrays and locals, not refs: the template never reads them.
const queue: OverlayEvent[] = []
let draining = false

// The leave animation is 260ms (.pop-leave-active); the rest is a beat of
// breathing room so two alerts don't read as one long one.
const GAP_MS = 450
// A 40-person raid shouldn't book six minutes of screen time. Overflow is
// dropped from the *display* only — it's still logged in the activity feed.
const MAX_QUEUE = 12

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

watch(() => store.lastEvent, (event) => {
  if (event?.type !== 'alert') return
  if (queue.length >= MAX_QUEUE) return
  queue.push(event)
  // One loop drains the whole queue, so only start it if it isn't running.
  if (!draining) void drain()
})

async function drain() {
  draining = true
  while (queue.length) {
    const event = queue.shift()!
    alertText.value = event.text ?? ''
    alertKind.value = event.kind ?? ''
    alertShowing.value = true
    // Fired here rather than on arrival: a raid would otherwise stack every
    // jingle into one second while the first alert is still on screen. Reads
    // through the computed, so it picks up the kind assigned just above.
    playSound(kindConfig.value.sound, store.config?.alerts.volume ?? FALLBACK_VOLUME)
    // Read inside the loop, so changing the duration in /control applies to
    // alerts still waiting rather than only to the next batch.
    await wait(store.config?.alerts.duration_ms ?? FALLBACK_DURATION_MS)
    alertShowing.value = false
    await wait(GAP_MS)
  }
  draining = false
}

onMounted(() => {
  store.connect()
  store.loadConfig()
  primeAudio()
})

// The loop would otherwise keep running after the view is gone, writing to refs
// nothing renders. OBS's browser source never unmounts, but navigating away
// from /overlay/alert in a normal tab does.
onUnmounted(() => {
  queue.length = 0
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
