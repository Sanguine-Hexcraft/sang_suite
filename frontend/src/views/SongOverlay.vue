<script setup lang="ts">
// The song request player (Phase 13b), its own OBS browser source.
//
// The one rule that shapes this file: **the overlay follows, the backend
// decides.** The queue lives in backend/songs.py. This page plays whatever
// `songs.now` says, pauses when `songs.paused` says, and when a song ends or
// fails it *reports* that (POST /api/songs/ended) and waits for the backend's
// new state, rather than picking the next song itself. That's what lets a
// skip from the dashboard, a refresh of this page and (later) `!skip` in chat
// all agree about what's playing.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useOverlayStore } from '@/stores/overlay'
import { loadYouTube, PlayerState, type YTPlayer } from '@/lib/youtube'

const store = useOverlayStore()

// The dashboard's slider, 0-100. Config rides the relay, so a slider drag
// reaches the player live.
const volume = computed(() => store.config?.songs?.volume ?? 50)

const now = computed(() => store.songs?.now ?? null)
// The card shows only while music is actually meant to be audible, so it can
// never claim a song is playing during a pause.
const visible = computed(() => !!now.value && !store.songs?.paused)

// The player is created imperatively inside this element. YT.Player *replaces*
// the node it's given with an iframe, so it gets a plain div made here rather
// than one Vue rendered -- Vue would otherwise try to patch or remove a node
// that's no longer there.
const frame = ref<HTMLDivElement | null>(null)

// The backend also refuses anything else; this is the second lock, the twin
// of MediaOverlay's /media/ check.
const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/

// Stall watchdog. Like the media overlay, it measures LACK OF PROGRESS, never
// elapsed time -- a fixed cap would cut every song longer than the cap.
const POLL_MS = 3_000
const STALL_MS = 15_000
// YouTube's minimum embed size is 200x200; 16:9 at 200px tall is 356 wide.
const PLAYER_W = 356
const PLAYER_H = 200

let player: YTPlayer | null = null
let ready = false
// The request_id loaded into the player, compared against `now` to know
// whether the backend has moved on.
let currentId: string | null = null
// The request we've already reported as over, and why. Kept so a report that
// failed (backend down) is re-sent once it's back, instead of the player
// restarting a song that already ended.
let reportedId: string | null = null
let reportedReason = 'ended'
// Whether the current song has actually started playing. Two things lean on
// it -- see onStateChange and poll().
let started = false
let lastTime = -1
let lastProgressAt = Date.now()
let pollTimer: ReturnType<typeof setInterval> | undefined
let retryTimer: ReturnType<typeof setTimeout> | undefined

/** Make the player match the backend's state. Called on every change. */
function sync() {
  if (!player || !ready) return
  const state = store.songs
  const song = state?.now ?? null

  if (!song) {
    if (currentId) player.stopVideo()
    currentId = null
    return
  }

  // Already reported this one as over; the backend just hasn't heard (it was
  // unreachable). Say it again rather than playing it a second time.
  if (song.request_id === reportedId) {
    void postEnded(song.request_id, reportedReason)
    return
  }

  if (song.request_id !== currentId) {
    currentId = song.request_id
    started = false
    lastTime = -1
    lastProgressAt = Date.now()
    if (!VIDEO_ID.test(song.video_id)) {
      report('error:bad-id')
      return
    }
    // Cue rather than load while paused, so a refresh mid-pause shows the
    // right song without starting it.
    if (state?.paused) player.cueVideoById(song.video_id)
    else player.loadVideoById(song.video_id)
    return
  }

  // Same song: only the paused flag can have changed. pauseVideo keeps the
  // position, so Resume carries on where it stopped rather than from the top.
  if (state?.paused) player.pauseVideo()
  else player.playVideo()
}

/** This song is over, for `reason`. Stop it and tell the backend. */
function report(reason: string) {
  if (!currentId || reportedId === currentId) return
  reportedId = currentId
  reportedReason = reason
  // Silence it now, not when the next song arrives: a stalled or refused song
  // must never be left half-playing (the media overlay's truncated clip kept
  // its audio going for exactly this reason).
  player?.stopVideo()
  void postEnded(currentId, reason)
}

async function postEnded(requestId: string, reason: string) {
  try {
    await fetch('/api/songs/ended', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: requestId, reason }),
    })
    // No need to read the reply. If we advanced the queue, the new state
    // arrives over /ws like everyone else's; if another overlay beat us to
    // it, that state already arrived.
  } catch {
    // Backend unreachable. sync() re-sends this when the state next arrives,
    // which a reconnect guarantees (loadSongs on connect).
  }
}

function onStateChange(e: { data: number }) {
  if (e.data === PlayerState.PLAYING) {
    started = true
    return
  }
  // Only once this song has really played. Loading a new video can deliver a
  // late ENDED from the previous one, and that must not skip the new song.
  if (e.data === PlayerState.ENDED && started) report('ended')
}

function onError(e: { data: number }) {
  // 101/150 (embedding refused) is the common one: label-owned videos often
  // say "embeddable" to the Data API and refuse anyway, or only in some
  // regions. The backend logs the code.
  console.warn('[song] player error', e.data, 'for', currentId)
  report(`error:${e.data}`)
}

/**
 * Every few seconds: is the song still moving?
 *
 * A song that has never started is deliberately *not* skipped. In OBS that
 * essentially never happens (the onError path covers bad videos), but in a
 * normal browser tab autoplay with sound is blocked until you click the page.
 * If never-started counted as a stall, a forgotten tab of this overlay would
 * skip every song in the queue after 15 seconds each, while OBS was playing
 * them fine. Only "it was playing and then stopped moving" counts.
 */
function poll() {
  if (!player || !ready || !currentId || reportedId === currentId) return
  if (store.songs?.paused) {
    // Not moving because we asked it not to.
    lastProgressAt = Date.now()
    return
  }
  const t = player.getCurrentTime()
  if (t !== lastTime) {
    lastTime = t
    lastProgressAt = Date.now()
    return
  }
  if (!started) return
  if (Date.now() - lastProgressAt > STALL_MS) {
    console.warn('[song] no progress for', STALL_MS, 'ms, skipping', currentId)
    report('stall')
  }
}

async function createPlayer() {
  try {
    const YT = await loadYouTube()
    if (!frame.value) return
    const host = document.createElement('div')
    frame.value.appendChild(host)
    player = new YT.Player(host, {
      width: PLAYER_W,
      height: PLAYER_H,
      playerVars: {
        controls: 0,
        disablekb: 1,
        fs: 0,
        rel: 0,
        iv_load_policy: 3, // no annotations
        playsinline: 1,
        // Some embeds refuse to play without a known origin. location.origin
        // is right in both modes: :5173 under Vite, :8000 on stream.
        origin: location.origin,
      },
      events: {
        onReady: () => {
          ready = true
          player?.setVolume(volume.value)
          sync()
        },
        onStateChange,
        onError,
      },
    })
  } catch (err) {
    // Usually no network when the browser source loaded. Try again; the
    // queue is safe on the backend meanwhile.
    console.error('[song]', err)
    retryTimer = setTimeout(createPlayer, 10_000)
  }
}

// The backend's state replaces store.songs wholesale on every push, so a
// shallow watch sees every change.
watch(() => store.songs, sync)

watch(volume, (v) => {
  if (ready) player?.setVolume(v)
})

// State is never replayed, so catch up on every (re)connect instead.
watch(
  () => store.connected,
  (up) => {
    if (up) void store.loadSongs()
  },
)

// The panic message lands a moment before the backend's paused state does;
// stopping on the message itself makes the button feel instant.
watch(
  () => store.lastEvent,
  (event) => {
    if (event?.type === 'panic') player?.pauseVideo()
  },
)

onMounted(() => {
  store.connect()
  void store.loadConfig()
  void store.loadSongs()
  void createPlayer()
  pollTimer = setInterval(poll, POLL_MS)
})

onUnmounted(() => {
  clearInterval(pollTimer)
  clearTimeout(retryTimer)
  player?.destroy()
  player = null
  ready = false
})
</script>

<template>
  <div class="stage">
    <Transition name="slide">
      <div v-show="visible" class="card">
        <div ref="frame" class="player" />
        <div class="info">
          <div class="label">NOW PLAYING</div>
          <!-- Titles are viewer-chosen text: plain interpolation, never v-html. -->
          <div class="title">{{ now?.title }}</div>
          <div class="meta">
            {{ now?.channel }} <span class="dot">·</span> requested by {{ now?.user }}
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

/* Bottom-left by default; move or crop it in OBS as you like. */
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
  /* Same "r g b" triple trick as the alert card, in the redeem teal. */
  --accent: 61 220 151;
  --border: 3px;
  /* base.css makes every element border-box, so a width here *includes* the
     border. Set to exactly 356px, the card had 350px inside for a 356px
     player, which then stuck out past the frame. */
  width: calc(356px + 2 * var(--border));
  font-family: 'Departure Mono', ui-monospace, monospace;
  color: #f2eefa;
  background: rgb(9 7 12 / 0.85);
  border: var(--border) solid rgb(var(--accent));
  box-shadow: 0 0 28px rgb(var(--accent) / 0.35);
}

.player {
  width: 356px;
  height: 200px;
  background: #000;
}

/* The iframe is made by YouTube's script, not by this template, so it has no
   scoped-style attribute: :deep() is how a scoped rule reaches it. Block, not
   the default inline, or it sits on a text baseline and gains a few pixels of
   gap underneath -- which is enough to overflow the 200px frame. */
.player :deep(iframe) {
  display: block;
  width: 100%;
  height: 100%;
}

.info {
  padding: 10px 12px 12px;
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
  /* Two lines at most; YouTube titles run long. */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.meta {
  margin-top: 6px;
  font-size: 11px;
  color: rgb(242 238 250 / 0.6);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dot {
  color: rgb(var(--accent));
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
