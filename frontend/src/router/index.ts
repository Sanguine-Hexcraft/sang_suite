import { createRouter, createWebHistory } from 'vue-router'
import ControlView from '../views/ControlView.vue'
import AlertOverlay from '../views/AlertOverlay.vue'
import MediaOverlay from '../views/MediaOverlay.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/control', component: ControlView },
    { path: '/overlay/alert', component: AlertOverlay },
    // Its own browser source in OBS: a clip and a follow alert sharing one
    // page would fight over the DOM and one audio context.
    { path: '/overlay/media', component: MediaOverlay },
  ],
})

export default router
