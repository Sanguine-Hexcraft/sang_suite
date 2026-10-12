<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useOverlayStore, type DeathEvent } from '@/stores/overlay'
import { playToll, primeAudio } from '@/audio/sounds'

// The death counter's own browser source (Phase 14). Everything it shows comes
// from store.deaths, which the store keeps current from both the HTTP seed and
// live `death`/`deaths` messages; this view only adds the theatre.
const store = useOverlayStore()

const FALLBACK_VOLUME = 0.45
// How long a test death from /control holds its fake +1 before the real count
// smokes back in.
const TEST_HOLD_MS = 6000
// The flare's length (see the CSS). Deaths closer together than this share
// one flare and one bell rather than stacking tolls on top of each other --
// a reconnect replays every missed death back to back, and two deaths can
// land in the same 2 s poll.
const FLARE_MS = 1800
let lastFlareAt = -Infinity

const flaring = ref(false)
// A test death previews the effect without touching the real count: +1 here,
// for TEST_HOLD_MS, never sent back to the backend.
const testBump = ref(0)
const testZone = ref('')

const shown = computed(() => (store.deaths?.count ?? 0) + testBump.value)
const epitaph = computed(() => {
  const zone = testBump.value ? testZone.value : store.deaths?.last?.zone
  return zone ? `Last death: ${zone}` : 'No deaths yet'
})

// Elder Futhark standing in for drow script, punctuated with the runic
// separator. textLength stretches it to exactly one lap of the ring.
const RUNES = 'ᚠᚢᚦᚨᚱᚲᚷᚹ᛫ᚺᚾᛁᛃᛇᛈᛉᛊ᛫ᛏᛒᛖᛗᛚᛜᛞᛟ᛫'

let testTimer: ReturnType<typeof setTimeout> | undefined

const plaque = ref<HTMLElement | null>(null)

// The flare is the `flaring` class: adding it plays the animations once, and
// the plaque's own animationend takes it off again (see the template). There
// used to be a setTimeout for the "off" and a requestAnimationFrame for the
// "on", and that pair could jam: OBS stops frame callbacks for a source that
// isn't on screen but keeps running timers, so a death off-scene had the
// timer switch the flare off *before* the frame switched it on -- leaving it
// on for good, and a class that's already there can't replay.
async function flare() {
  const now = performance.now()
  if (now - lastFlareAt < FLARE_MS) return
  lastFlareAt = now
  // To restart an animation that's mid-flight, the browser has to *see* the
  // class gone. Swapping it out and back within one frame doesn't count:
  // styles are only recalculated before paint, so it never notices the gap.
  flaring.value = false
  await nextTick() // the DOM no longer has the class...
  void plaque.value?.offsetWidth // ...and reading layout forces a recalc now
  flaring.value = true
  playToll(store.config?.alerts.volume ?? FALLBACK_VOLUME)
}

watch(() => store.lastEvent, (event) => {
  if (event?.type === 'panic') {
    clearTimeout(testTimer)
    testBump.value = 0
    flaring.value = false
    lastFlareAt = -Infinity
    return
  }
  if (event?.type !== 'death') return
  const death = event as DeathEvent
  if (death.test) {
    testBump.value = 1
    testZone.value = death.zone
    clearTimeout(testTimer)
    testTimer = setTimeout(() => (testBump.value = 0), TEST_HOLD_MS)
  }
  void flare()
})

// Re-seed on every (re)connect, not just on mount: a reset pressed while the
// socket was down is state, not an event, so it is never replayed.
watch(() => store.connected, (up) => {
  if (up) void store.loadDeaths()
})

onMounted(() => {
  store.connect()
  store.loadConfig()
  void store.loadDeaths()
  primeAudio()
})

onUnmounted(() => {
  clearTimeout(testTimer)
})
</script>


<template>
  <!-- Held back until the real count arrives: rendering a placeholder 0 first
       would play the smoke transition 0 -> N on every source refresh. -->
  <!-- animationend bubbles, and the spider and ring flare inside this div;
       .self listens only for the plaque's own animation. All three run the
       same 1.8s, so the plaque's ending is everyone's ending. A hidden source
       doesn't animate, so this simply fires later -- it can't fire early. -->
  <div
    v-if="store.deaths"
    ref="plaque"
    class="plaque"
    :class="{ flaring }"
    @animationend.self="flaring = false"
  >
    <div class="sigil" aria-hidden="true">
      <!-- Two wrappers so the slow drift and the one-shot burst each own a
           transform; animating both on one element would make them fight. -->
      <div class="ring-drift">
        <div class="ring-burst">
          <svg class="ring" viewBox="0 0 120 120">
            <defs>
              <path id="rune-lap" d="M60,8 a52,52 0 1,1 -0.01,0" />
            </defs>
            <circle cx="60" cy="60" r="58" class="rule" />
            <circle cx="60" cy="60" r="45" class="rule" />
            <text class="runes">
              <textPath href="#rune-lap" textLength="324" lengthAdjust="spacing">{{ RUNES }}</textPath>
            </text>
          </svg>
        </div>
      </div>

      <svg class="spider" viewBox="0 0 120 120">
        <!-- the web: spokes plus three octagonal turns -->
        <g class="web">
          <path d="M60,16V104M16,60H104M29,29L91,91M91,29L29,91" />
          <polygon points="60,46 70,50 74,60 70,70 60,74 50,70 46,60 50,50" />
          <polygon points="60,33 79,41 87,60 79,79 60,87 41,79 33,60 41,41" />
          <polygon points="60,20 88,32 100,60 88,88 60,100 32,88 20,60 32,32" />
        </g>
        <!-- legs drawn once and mirrored, so the two sides can't disagree -->
        <g class="legs">
          <path d="M55,47 L41,34 L35,19" />
          <path d="M54,50 L37,43 L24,38" />
          <path d="M54,53 L37,59 L26,70" />
          <path d="M55,56 L43,71 L38,88" />
          <g transform="translate(120 0) scale(-1 1)">
            <path d="M55,47 L41,34 L35,19" />
            <path d="M54,50 L37,43 L24,38" />
            <path d="M54,53 L37,59 L26,70" />
            <path d="M55,56 L43,71 L38,88" />
          </g>
        </g>
        <ellipse class="body" cx="60" cy="70" rx="11" ry="14" />
        <circle class="body" cx="60" cy="51" r="7" />
        <circle class="body" cx="60" cy="42" r="3.5" />
        <path class="hourglass" d="M56,63 L64,63 L60,70 L64,77 L56,77 L60,70 Z" />
        <circle class="eye" cx="58.4" cy="41.2" r="0.9" />
        <circle class="eye" cx="61.6" cy="41.2" r="0.9" />
      </svg>
    </div>

    <div class="inscription">
      <p class="label">Deaths</p>
      <div class="numeral-wrap">
        <Transition name="smoke">
          <span :key="shown" class="numeral">{{ shown }}</span>
        </Transition>
      </div>
      <p class="epitaph">{{ epitaph }}</p>
    </div>
  </div>
</template>


<style scoped>
/* All four bundled under public/fonts (OFL, from Google Fonts) so an OBS
   browser source never waits on the network. local() first, as elsewhere. */
@font-face {
  font-family: 'Cinzel Decorative';
  src: local('Cinzel Decorative Bold'), url('/fonts/CinzelDecorative-Bold.woff2') format('woff2');
  font-weight: 700;
  font-display: block;
}
@font-face {
  font-family: 'UnifrakturCook';
  src: local('UnifrakturCook'), url('/fonts/UnifrakturCook-Bold.woff2') format('woff2');
  font-weight: 700;
  font-display: block;
}
@font-face {
  font-family: 'IM Fell English';
  src: local('IM FELL English Italic'), url('/fonts/IMFellEnglish-Italic.woff2') format('woff2');
  font-style: italic;
  font-display: block;
}
@font-face {
  font-family: 'Noto Sans Runic';
  src: local('Noto Sans Runic'), url('/fonts/NotoSansRunic-Regular.woff2') format('woff2');
  font-display: block;
}

/* Overlays must be see-through; scoped styles can't reach body. */
:global(body) {
  background: transparent;
  margin: 0;
  overflow: hidden;
}

.plaque {
  --obsidian: 11 7 16;
  --deep: 42 15 61;
  --violet: 155 92 255;
  --bone: 217 207 232;
  --ember: 196 60 106;

  /* Inset from the corner so the outer glow isn't clipped by the browser
     source's edge. Size the OBS source to about 420 x 180. */
  position: fixed;
  top: 18px;
  left: 18px;
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 14px 28px 14px 16px;
  min-width: 330px;
  border: 1px solid rgb(var(--violet) / 0.35);
  border-radius: 3px;
  background:
    radial-gradient(ellipse at 22% 50%, rgb(var(--deep) / 0.92), rgb(var(--obsidian) / 0.9) 68%);
  box-shadow:
    inset 0 0 26px rgb(0 0 0 / 0.85),
    0 0 18px rgb(var(--violet) / 0.16);
  color: rgb(var(--bone));
}

/* A second, fainter rule inside the border: the frame of an old plate. */
.plaque::before {
  content: '';
  position: absolute;
  inset: 5px;
  border: 1px solid rgb(var(--violet) / 0.12);
  border-radius: 2px;
  pointer-events: none;
}

.plaque.flaring {
  animation: plaque-flash 1.8s ease-out;
}

/* --- the sigil --------------------------------------------------------- */
.sigil {
  position: relative;
  flex: none;
  width: 116px;
  height: 116px;
}

.ring-drift,
.ring-burst,
.ring,
.spider {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.ring-drift {
  animation: drift 120s linear infinite;
}

/* Against the drift, so a death reads as the ring being wrenched. */
.flaring .ring-burst {
  animation: burst 1.8s cubic-bezier(0.2, 0.7, 0.2, 1);
}

.rule {
  fill: none;
  stroke: rgb(var(--violet) / 0.22);
  stroke-width: 0.6;
}

.runes {
  font-family: 'Noto Sans Runic', serif;
  font-size: 9px;
  fill: rgb(var(--violet) / 0.55);
  animation: candle-faint 5.3s infinite;
}

.spider {
  color: rgb(var(--violet));
  filter: drop-shadow(0 0 3px rgb(var(--violet) / 0.6));
}

.web {
  fill: none;
  stroke: rgb(var(--violet) / 0.14);
  stroke-width: 0.5;
}

.legs {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.body {
  fill: rgb(var(--obsidian));
  stroke: currentColor;
  stroke-width: 1.4;
}

.hourglass {
  fill: rgb(var(--ember));
}

.eye {
  fill: currentColor;
}

.flaring .spider {
  animation: spider-flare 1.8s ease-out;
}

/* --- the inscription ----------------------------------------------------- */
.inscription {
  display: flex;
  flex-direction: column;
  min-width: 150px;
}

.label,
.epitaph {
  margin: 0;
}

.label {
  font-family: 'Cinzel Decorative', serif;
  font-weight: 700;
  font-size: 13px;
  letter-spacing: 0.1em;
  color: rgb(var(--bone) / 0.78);
}

.numeral-wrap {
  position: relative;
  height: 70px;
}

.numeral {
  display: inline-block;
  font-family: 'UnifrakturCook', serif;
  font-weight: 700;
  font-size: 66px;
  line-height: 70px;
  animation: candle 3.7s infinite;
}

.epitaph {
  font-family: 'IM Fell English', serif;
  font-style: italic;
  font-size: 14px;
  color: rgb(var(--bone) / 0.62);
}

/* The old number dissolves upward like smoke while the new one rises in. */
.smoke-leave-active {
  position: absolute;
  left: 0;
  top: 0;
  transition: opacity 0.8s ease-in, transform 0.8s ease-in, filter 0.8s ease-in;
}
.smoke-enter-active {
  transition: opacity 0.9s ease 0.35s, transform 0.9s ease 0.35s, filter 0.9s ease 0.35s;
}
.smoke-leave-to {
  opacity: 0;
  transform: translateY(-22px) scale(1.15);
  filter: blur(8px);
}
.smoke-enter-from {
  opacity: 0;
  transform: translateY(14px) scale(0.9);
  filter: blur(6px);
}

/* --- keyframes ------------------------------------------------------------ */
/* Uneven stops on purpose: an evenly spaced flicker reads as a strobe. */
@keyframes candle {
  0%, 100% {
    opacity: 1;
    text-shadow: 0 0 6px rgb(var(--violet) / 0.9), 0 0 12px rgb(var(--violet) / 0.4);
  }
  11% {
    opacity: 0.9;
    text-shadow: 0 0 4px rgb(var(--violet) / 0.7), 0 0 13px rgb(var(--violet) / 0.3);
  }
  17% { opacity: 1; }
  42% {
    opacity: 0.84;
    text-shadow: 0 0 3px rgb(var(--violet) / 0.6), 0 0 10px rgb(var(--violet) / 0.25);
  }
  47% {
    opacity: 0.97;
    text-shadow: 0 0 7px rgb(var(--violet) / 0.95), 0 0 14px rgb(var(--violet) / 0.45);
  }
  71% { opacity: 0.93; }
  83% {
    opacity: 0.88;
    text-shadow: 0 0 5px rgb(var(--violet) / 0.75), 0 0 16px rgb(var(--violet) / 0.35);
  }
}

@keyframes candle-faint {
  0%, 100% { opacity: 1; }
  23% { opacity: 0.6; }
  29% { opacity: 0.9; }
  61% { opacity: 0.7; }
  68% { opacity: 1; }
}

@keyframes drift {
  to { transform: rotate(360deg); }
}

@keyframes burst {
  to { transform: rotate(-360deg); }
}

@keyframes spider-flare {
  0% {
    color: rgb(var(--ember));
    filter: drop-shadow(0 0 12px rgb(var(--ember))) brightness(1.5);
  }
  100% {
    color: rgb(var(--violet));
    filter: drop-shadow(0 0 3px rgb(var(--violet) / 0.6));
  }
}

@keyframes plaque-flash {
  0% {
    border-color: rgb(var(--ember) / 0.9);
    box-shadow:
      inset 0 0 26px rgb(0 0 0 / 0.85),
      0 0 30px rgb(var(--ember) / 0.55);
  }
}

@media (prefers-reduced-motion: reduce) {
  .ring-drift,
  .flaring .ring-burst,
  .runes,
  .numeral {
    animation: none;
  }
  .smoke-enter-active,
  .smoke-leave-active {
    transition-duration: 0.2s;
    transition-delay: 0s;
  }
}
</style>
