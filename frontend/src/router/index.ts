import { createRouter, createWebHistory } from 'vue-router'
import ControlView from '../views/ControlView.vue'
import AlertOverlay from '../views/AlertOverlay.vue'
import MediaOverlay from '../views/MediaOverlay.vue'
import DeathOverlay from '../views/DeathOverlay.vue'
import ZoneOverlay from '../views/ZoneOverlay.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/control', component: ControlView },
    { path: '/overlay/alert', component: AlertOverlay },
    // Its own browser source in OBS: a clip and a follow alert sharing one
    // page would fight over the DOM and one audio context.
    { path: '/overlay/media', component: MediaOverlay },
    // Monsters & Memories death counter (Phase 14): a corner widget, so its
    // own source sized to the plaque rather than a full-canvas page.
    { path: '/overlay/deaths', component: DeathOverlay },
    // M&M zone banner: a lower-third, its own source pinned to the bottom.
    { path: '/overlay/zone', component: ZoneOverlay },
  ],
})

export default router
