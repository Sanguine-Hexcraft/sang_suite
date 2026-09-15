<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useOverlayStore, type MediaEvent } from '@/stores/overlay'

const store = useOverlayStore()

// The element is driven imperatively (play/pause/src) rather than through a
// reactive `src` binding: we need to know exactly when a load finishes so the
// next clip can be preloaded, and v-bind gives no hook for that.
const video = ref<HTMLVideoElement | null>(null)
const image = ref<HTMLImageElement | null>(null)

const showing = ref(false)
// Images have no `ended` event, so they're a separate branch with a timer.
const currentImage = ref<string | null>(null)

// Watchdog for a clip that dies mid-playback: `ended` never fires, so without
// this the queue would wedge forever. It measures LACK OF PROGRESS, not
// elapsed time -- `timeupdate` fires several times a second while a video is
// actually playing, and every one of them resets this. A fixed cap on total
// playback would truncate any clip longer than the cap, which is a bug I
// already shipped once: pumps.webm is 75s and got cut at 30s.
const STALL_TIMEOUT_MS = 15_000
// How long a still image (gif/webp) stays up. GIFs loop with no end event, so
// this is the only thing that can end them.
const IMAGE_MS = 6_000
const GAP_MS = 250
const MAX_QUEUE = 12

const queue: MediaEvent[] = []
let draining = false
let advance: (() => void) | null = null

const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const isImage = (src: string) => /\.(gif|webp|png|jpe?g)$/i.test(src)

watch(
  () => store.lastEvent,
  (event) => {
    if (event?.type === 'panic') {
      queue.length = 0
      stopCurrent()
      return
    }
    if (event?.type !== 'media') return
    const media = event as MediaEvent
    if (media.interrupt) {
      // Cut in: drop what's on screen and jump the queue rather than waiting.
      queue.unshift(media)
      stopCurrent()
    } else {
      if (queue.length >= MAX_QUEUE) return
      queue.push(media)
    }
    if (!draining) void drain()
  },
)

function stopCurrent() {
  currentImage.value = null
  showing.value = false
  // Releases whatever `drain` is awaiting so the loop moves on immediately.
  // `finish` owns tearing the <video> down, so this needs no element handling
  // of its own -- and must not duplicate it, or the two paths can drift.
  if (advance) {
    advance()
    return
  }
  // Nothing was mid-play (panic on an idle overlay); still make sure the
  // element is quiet.
  video.value?.pause()
}

/** Resolves when the clip ends, errors, or stops making progress. */
function playOne(media: MediaEvent): Promise<void> {
  return new Promise((resolve) => {
    let done = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const finish = () => {
      if (done) return
      done = true
      clearTimeout(timer)
      advance = null
      // Stop the element before resolving. Hiding it with v-show leaves it
      // playing -- that is how a truncated clip kept blaring its audio.
      const el = video.value
      if (el) {
        el.pause()
        el.ontimeupdate = null
        el.onended = null
        el.onerror = null
        el.removeAttribute('src')
        el.load()
      }
      resolve()
    }
    advance = finish

    if (isImage(media.src)) {
      currentImage.value = media.src
      showing.value = true
      // Gifs loop forever with no `ended` event, so a timer is the only thing
      // that can end them. Safe here precisely because there is no playback to
      // truncate -- unlike the video branch.
      timer = setTimeout(finish, IMAGE_MS)
      return
    }

    const el = video.value
    if (!el) return finish()

    const kick = () => {
      clearTimeout(timer)
      timer = setTimeout(finish, STALL_TIMEOUT_MS)
    }

    el.src = media.src
    el.onended = finish
    // Proof of life: resets the watchdog for as long as the clip is actually
    // advancing, so length never matters, only stalling.
    el.ontimeupdate = kick
    // A missing file or a codec OBS can't decode must not wedge the queue.
    el.onerror = () => {
      console.error('[media] failed to play', media.src)
      finish()
    }
    showing.value = true
    kick() // covers the gap before the first timeupdate arrives
    // Autoplay with sound needs the browser source to allow it; OBS does.
    void el.play().catch((err) => {
      console.error('[media] play() rejected', err)
      finish()
    })
  })
}

/** Fetches the next clip into cache so back-to-back plays have no black gap. */
function preloadNext() {
  const next = queue[0]
  if (!next || isImage(next.src)) return
  const pre = document.createElement('video')
  pre.preload = 'auto'
  pre.src = next.src
}

async function drain() {
  draining = true
  while (queue.length) {
    const media = queue.shift()!
    preloadNext()
    await playOne(media)
    showing.value = false
    currentImage.value = null
    await wait(GAP_MS)
  }
  draining = false
}

onMounted(() => {
  store.connect()
  store.loadConfig()
})

onUnmounted(() => {
  queue.length = 0
  stopCurrent()
})
</script>

<template>
  <div class="stage">
    <Transition name="fade">
      <div v-show="showing" class="frame">
        <video ref="video" v-show="!currentImage" class="clip" playsinline />
        <img v-if="currentImage" ref="image" :src="currentImage" class="clip" alt="" />
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
.stage {
  position: fixed;
  inset: 0;
  display: grid;
  place-items: center;
  pointer-events: none;
}

.frame {
  display: grid;
  place-items: center;
  max-width: 100vw;
  max-height: 100vh;
}

.clip {
  max-width: 100vw;
  max-height: 100vh;
  object-fit: contain;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 180ms ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
