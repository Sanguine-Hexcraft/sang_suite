// The YouTube IFrame Player API (Phase 13b), loaded on demand.
//
// Why the API rather than a bare <iframe src="youtube.com/embed/...">: a bare
// iframe plays, but it can't tell us anything. The song queue needs to know
// when a song *ended* (to advance) and when it *failed* (embedding refused,
// video removed), and only the API's events say so.
//
// The types are declared here by hand, covering only what the song overlay
// calls, rather than pulling in @types/youtube for a dozen lines.

/** YT.PlayerState, as the numbers onStateChange reports. */
export const PlayerState = {
  UNSTARTED: -1,
  ENDED: 0,
  PLAYING: 1,
  PAUSED: 2,
  BUFFERING: 3,
  CUED: 5,
} as const

export interface YTPlayer {
  loadVideoById(videoId: string): void // load and start playing
  cueVideoById(videoId: string): void // load but wait for playVideo()
  playVideo(): void
  pauseVideo(): void
  stopVideo(): void
  getCurrentTime(): number
  getPlayerState(): number
  setVolume(volume: number): void // 0-100
  destroy(): void
}

export interface YTPlayerOptions {
  width?: number
  height?: number
  videoId?: string
  playerVars?: Record<string, string | number>
  events?: {
    onReady?: () => void
    onStateChange?: (e: { data: number }) => void
    // e.data: 2 bad id, 5 HTML5 player error, 100 removed/private,
    // 101 and 150 the owner disabled embedding.
    onError?: (e: { data: number }) => void
  }
}

export interface YTNamespace {
  Player: new (el: HTMLElement, options: YTPlayerOptions) => YTPlayer
}

declare global {
  interface Window {
    YT?: YTNamespace
    // The API script calls this global once it has finished setting up. It's
    // the only "ready" signal it gives: the script's own onload fires before
    // YT.Player exists.
    onYouTubeIframeAPIReady?: () => void
  }
}

let loading: Promise<YTNamespace> | null = null

/** Inject the API script once and resolve when YT.Player is usable. */
export function loadYouTube(): Promise<YTNamespace> {
  if (loading) return loading
  loading = new Promise((resolve, reject) => {
    if (window.YT?.Player) return resolve(window.YT)
    const previous = window.onYouTubeIframeAPIReady
    window.onYouTubeIframeAPIReady = () => {
      previous?.()
      resolve(window.YT!)
    }
    const tag = document.createElement('script')
    tag.src = 'https://www.youtube.com/iframe_api'
    tag.onerror = () => {
      // Forget the failed attempt so the caller can retry later (no network
      // when OBS started, say) instead of getting the same rejection forever.
      loading = null
      tag.remove()
      reject(new Error('could not load the YouTube player API'))
    }
    document.head.appendChild(tag)
  })
  return loading
}
