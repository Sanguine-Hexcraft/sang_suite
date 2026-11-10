<script setup lang="ts">
// Spotify "now playing" (Phase 14b), its own OBS browser source.
//
// Display only. The music is the Spotify desktop app's, heard through desktop
// audio; this page just shows what backend/spotify.py reads over MPRIS. That
// makes it far simpler than SongOverlay: there's no player to drive and
// nothing to report back.
//
// The backend sends a message only when something *changes* (new track,
// play/pause, a seek), each carrying the position at that moment. Everything
// between messages is this page counting forward by itself.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useOverlayStore } from '@/stores/overlay'

const store = useOverlayStore()

const state = computed(() => store.spotify)
const track = computed(() => state.value?.track ?? null)
// Only while music is actually audible, like the song card: a paused Spotify
// (including one the backend paused for a song request) shows nothing.
const visible = computed(() => !!track.value && !!state.value?.playing)

// The backend only passes art from Spotify's CDN; this is the second lock,
// the twin of MediaOverlay's /media/ check.
const art = computed(() => {
  const url = track.value?.art
  return url && url.startsWith('https://i.scdn.co/') ? url : null
})

const artists = computed(() => track.value?.artists.join(', ') ?? '')

// --- the progress bar: one CSS animation, no timer --------------------------
// The bar is an animation as long as the track, started part-way through by a
// *negative* delay equal to the position. The browser then runs it on its own
// clock -- which matters in OBS, where a source that's off-scene stops drawing
// frames but time keeps passing; a bar advanced by a timer per frame would
// come back in the wrong place, and this one can't.
//
// `:key` on the bar is what restarts it: every message from the backend is a
// new `received` stamp, so Vue replaces the element and the animation begins
// again from the new position (that's how a seek lands).
const barStyle = computed(() => {
  const s = state.value
  if (!s?.track) return {}
  return {
    animationDuration: `${Math.max(s.track.duration_ms, 1)}ms`,
    animationDelay: `-${Math.min(s.position_ms, s.track.duration_ms)}ms`,
  }
})

// --- the elapsed time: text can't be animated, so this one does tick ---------
// The timer doesn't *count*; it only asks "what time is it now?" once a
// second. The answer is always worked out from the last message's position
// plus how long ago it arrived, so a late or skipped tick can't make it drift.
const nowMs = ref(performance.now())
let ticker: ReturnType<typeof setInterval> | undefined

const elapsed = computed(() => {
  const s = state.value
  if (!s?.track) return 0
  const ms = s.position_ms + (s.playing ? nowMs.value - s.received : 0)
  return Math.max(0, Math.min(ms, s.track.duration_ms))
})

function clock(ms: number) {
  const total = Math.floor(ms / 1000)
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

// A fresh message means a fresh `received`; bring "now" level with it at
// once, or for up to a second the sum above would use a stale now.
watch(state, () => (nowMs.value = performance.now()))

// State is never replayed, so catch up on every (re)connect instead.
watch(
  () => store.connected,
  (up) => {
    if (up) void store.loadSpotify()
  },
)

onMounted(() => {
  store.connect()
  void store.loadSpotify()
  ticker = setInterval(() => (nowMs.value = performance.now()), 1000)
})

onUnmounted(() => clearInterval(ticker))
</script>

<template>
  <div class="stage">
    <Transition name="slide">
      <div v-if="visible && track" class="card">
        <div class="row">
          <!-- :key so a new track swaps the image rather than showing the old
               cover until the new one has loaded. -->
          <img v-if="art" :key="art" class="art" :src="art" alt="" />
          <div v-else class="art blank">♪</div>
          <div class="info">
            <div class="label">NOW PLAYING</div>
            <div class="title">{{ track.title }}</div>
            <div class="artist">{{ artists }}</div>
            <div class="album">{{ track.album }}</div>
          </div>
        </div>
        <div class="progress">
          <div class="track">
            <div :key="state?.received" class="bar" :style="barStyle" />
          </div>
          <div class="times">
            <span>{{ clock(elapsed) }}</span>
            <span>{{ clock(track.duration_ms) }}</span>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style>
/* Browser sources must be transparent, and `scoped` cannot reach body. */
body {
  margin: 0;
  background: transparent;
}
</style>

<style scoped>
@font-face {
  font-family: 'Departure Mono';
  src: local('Departure Mono'), url('/fonts/DepartureMono-Regular.otf') format('opentype');
  font-display: block;
}

/* Bottom-left by default, same corner as the song request card: only one of
   the two is ever up, since a request pauses Spotify. Move it in OBS. */
.stage {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: flex-end;
  justify-content: flex-start;
  padding: 24px;
  box-sizing: border-box;
  pointer-events: none;
}

.card {
  /* Same "r g b" triple trick as the other cards, in Spotify's green. */
  --accent: 30 215 96;
  width: 420px;
  font-family: 'Departure Mono', ui-monospace, monospace;
  color: #f2eefa;
  background: rgb(9 7 12 / 0.85);
  border: 3px solid rgb(var(--accent));
  box-shadow: 0 0 28px rgb(var(--accent) / 0.35);
}

.row {
  display: flex;
  gap: 12px;
  padding: 12px 12px 10px;
}

.art {
  flex: none;
  width: 96px;
  height: 96px;
  object-fit: cover;
  background: #000;
}

.art.blank {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 40px;
  color: rgb(var(--accent) / 0.6);
}

.info {
  /* A flex child won't shrink below its content without this, and the
     ellipsis rules below would never kick in. */
  min-width: 0;
  flex: 1;
}

.label {
  font-size: 11px;
  letter-spacing: 0.18em;
  color: rgb(var(--accent));
}

.title {
  margin-top: 4px;
  font-size: 15px;
  line-height: 1.3;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.artist,
.album {
  margin-top: 4px;
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.album {
  font-size: 11px;
  color: rgb(242 238 250 / 0.6);
}

.progress {
  padding: 0 12px 10px;
}

.track {
  height: 4px;
  background: rgb(242 238 250 / 0.15);
  overflow: hidden;
}

.bar {
  height: 100%;
  background: rgb(var(--accent));
  /* Scaled, not resized: transform animates without relayout. `both` holds
     the full bar at the end instead of snapping back to empty. */
  transform-origin: left;
  animation: fill linear both;
}

@keyframes fill {
  from {
    transform: scaleX(0);
  }
  to {
    transform: scaleX(1);
  }
}

.times {
  display: flex;
  justify-content: space-between;
  margin-top: 5px;
  font-size: 10px;
  color: rgb(242 238 250 / 0.6);
}

.slide-enter-active,
.slide-leave-active {
  transition:
    opacity 220ms ease,
    transform 220ms ease;
}

.slide-enter-from,
.slide-leave-to {
  opacity: 0;
  transform: translateY(12px);
}
</style>
