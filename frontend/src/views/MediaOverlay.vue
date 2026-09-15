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

// A clip that fails to load or stalls would otherwise wedge the queue forever,
// since `ended` never fires. Generous enough not to truncate a real clip.
const STALL_TIMEOUT_MS = 30_000
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
  const el = video.value
  if (el) {
    el.pause()
    el.removeAttribute('src')
    el.load()
  }
  currentImage.value = null
  showing.value = false
  // Releases whatever `drain` is awaiting so the loop moves on immediately.
  advance?.()
}

/** Resolves when the clip ends, errors, or the stall timeout fires. */
function playOne(media: MediaEvent): Promise<void> {
  return new Promise((resolve) => {
    let done = false
    const finish = () => {
      if (done) return
      done = true
      clearTimeout(timer)
      advance = null
      resolve()
    }
    advance = finish
    const timer = setTimeout(finish, STALL_TIMEOUT_MS)

    if (isImage(media.src)) {
      currentImage.value = media.src
      showing.value = true
      setTimeout(finish, IMAGE_MS)
      return
    }

    const el = video.value
    if (!el) return finish()
    el.src = media.src
    el.onended = finish
    // A missing file or a codec OBS can't decode must not wedge the queue.
    el.onerror = () => {
      console.error('[media] failed to play', media.src)
      finish()
    }
    showing.value = true
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
