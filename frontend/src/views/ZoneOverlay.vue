<script setup lang="ts">
import { ref, onMounted, watch, nextTick } from 'vue'
import { useOverlayStore, type ZoneEvent } from '@/stores/overlay'

// The zone banner's own browser source: a lower-third that rises out of the
// dark when a Monsters & Memories zone finishes loading (backend/zones.py),
// holds, and sinks back. Names and lore lines come from backend/zones.json.
const store = useOverlayStore()

const name = ref('')
const lore = ref('')
const showing = ref(false)
const banner = ref<HTMLElement | null>(null)

// One CSS animation is the banner's whole life -- rise, hold, fade -- and its
// animationend puts it away (see the template). No timers: OBS keeps timers
// running for a source that's off screen but stops its frames, and the death
// plaque learned the hard way what a timer and an animation disagreeing does.
async function show(zone: ZoneEvent) {
  name.value = zone.name
  lore.value = zone.lore
  // Zoning again mid-banner restarts it with the new name. The browser has to
  // see the class gone to replay an animation, hence the forced reflow.
  showing.value = false
  await nextTick()
  void banner.value?.offsetWidth
  showing.value = true
}

watch(() => store.lastEvent, (event) => {
  if (event?.type === 'panic') {
    showing.value = false
    return
  }
  // A replayed zone-in is a banner for somewhere you've since left.
  if (event?.type !== 'zone' || event.replay) return
  void show(event as ZoneEvent)
})

onMounted(() => {
  store.connect()
})
</script>

<template>
  <div class="stage">
    <div
      v-show="showing"
      ref="banner"
      class="banner"
      :class="{ showing }"
      @animationend.self="showing = false"
    >
      <p class="runes" aria-hidden="true">ᚱ ᛫ ᛟ ᛫ ᚦ</p>
      <h1 class="name">{{ name }}</h1>
      <div class="rule" aria-hidden="true"><span class="gem" /></div>
      <p v-if="lore" class="lore">{{ lore }}</p>
    </div>
  </div>
</template>

<style scoped>
/* Bundled under public/fonts; same faces as the death plaque. */
@font-face {
  font-family: 'Cinzel Decorative';
  src: local('Cinzel Decorative Bold'), url('/fonts/CinzelDecorative-Bold.woff2') format('woff2');
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

:global(body) {
  background: transparent;
  margin: 0;
  overflow: hidden;
}

/* Size the OBS source to 1920 x 300 and pin it to the bottom of the canvas. */
.stage {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding-bottom: 48px;
}

.banner {
  --obsidian: 11 7 16;
  --deep: 42 15 61;
  --violet: 155 92 255;
  --bone: 217 207 232;

  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 10px 90px 14px;
  /* A pool of dark behind the text rather than a box: it has to read over
     a sunny beach and a black crypt alike. */
  background: radial-gradient(
    ellipse at 50% 55%,
    rgb(var(--deep) / 0.85),
    rgb(var(--obsidian) / 0.6) 45%,
    transparent 72%
  );
  color: rgb(var(--bone));
  text-align: center;
  opacity: 0;
}

.banner.showing {
  animation: banner-life 8s ease-in-out forwards;
}

.runes {
  margin: 0;
  font-family: 'Noto Sans Runic', serif;
  font-size: 15px;
  letter-spacing: 0.6em;
  color: rgb(var(--violet) / 0.7);
}

.name {
  margin: 2px 0 6px;
  font-family: 'Cinzel Decorative', serif;
  font-weight: 700;
  font-size: 58px;
  line-height: 1.1;
  letter-spacing: 0.06em;
  text-shadow:
    0 0 14px rgb(var(--violet) / 0.55),
    0 2px 4px rgb(0 0 0 / 0.9);
}

.showing .name {
  animation: unfurl 8s ease-out forwards;
}

/* Two hairlines meeting at a small violet lozenge. */
.rule {
  position: relative;
  width: 420px;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    rgb(var(--violet) / 0.75) 30%,
    rgb(var(--violet) / 0.75) 70%,
    transparent
  );
}

.gem {
  position: absolute;
  top: -4px;
  left: calc(50% - 4px);
  width: 8px;
  height: 8px;
  background: rgb(var(--violet));
  transform: rotate(45deg);
  box-shadow: 0 0 8px rgb(var(--violet) / 0.9);
}

.lore {
  margin: 10px 0 0;
  max-width: 900px;
  font-family: 'IM Fell English', serif;
  font-style: italic;
  font-size: 25px;
  text-shadow: 0 1px 3px rgb(0 0 0 / 0.95);
}

/* 8 s: rise for ~0.9 s, hold, then sink away over the last ~1.4 s. */
@keyframes banner-life {
  0% {
    opacity: 0;
    transform: translateY(14px);
    filter: blur(6px);
  }
  11% {
    opacity: 1;
    transform: none;
    filter: none;
  }
  82% {
    opacity: 1;
    transform: none;
    filter: none;
  }
  100% {
    opacity: 0;
    transform: translateY(-6px);
    filter: blur(3px);
  }
}

/* The name breathes out from tight-set to its resting spacing. */
@keyframes unfurl {
  0% {
    letter-spacing: -0.04em;
  }
  25%,
  100% {
    letter-spacing: 0.06em;
  }
}
</style>
