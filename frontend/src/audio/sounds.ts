// Synthesised alert jingles. Every one is the same engine — a short run of
// notes on one oscillator — so a "sound" is just data: which notes, how fast,
// and how the last one lands. Nothing is bundled as an audio file, so there's
// no binary asset to ship and the tunes stay tweakable right here.

let ctx: AudioContext | null = null

// Equal-temperament pitches, named so the tunes below read as music.
const C4 = 261.63
const G4 = 392.0
const C5 = 523.25
const E5 = 659.25
const G5 = 783.99
const B5 = 987.77
const C6 = 1046.5
const E6 = 1318.51

export interface Jingle {
  notes: number[] // played in order, one per step
  step: number // seconds between note onsets (also each note's length)
  tail: number // the final note rings this long instead
  // Doubles the final note an octave up: makes the phrase land as an arrival
  // rather than just one more blip. Wrong for the two-note flourishes.
  sparkle: boolean
}

// The original: a major triad resolving up an octave. Bound separately so the
// fallback below is a value TypeScript knows exists, not a Record lookup.
const LEVELUP: Jingle = { notes: [C5, E5, G5, C6], step: 0.075, tail: 0.34, sparkle: true }

export const SOUNDS: Record<string, Jingle> = {
  levelup: LEVELUP,
  // Follows are the most frequent alert, so this one stays out of the way.
  follow: { notes: [G5, C6], step: 0.07, tail: 0.18, sparkle: false },
  // Repeated notes into a resolution — the shape of a fanfare.
  sub: { notes: [G5, G5, G5, C6], step: 0.09, tail: 0.45, sparkle: true },
  // Bright, coin-like, and high enough to cut through game audio.
  cheer: { notes: [B5, E6], step: 0.08, tail: 0.5, sparkle: false },
  // Starts an octave and a half down and climbs: the longest of the set.
  raid: { notes: [C4, G4, C5, E5, G5, C6], step: 0.06, tail: 0.4, sparkle: true },
}

export const SOUND_NAMES = Object.keys(SOUNDS)
const DEFAULT_SOUND = 'levelup'

function context() {
  if (!ctx) ctx = new AudioContext()
  return ctx
}

/**
 * Browsers start an AudioContext suspended until the page has seen a user
 * gesture. OBS's browser source is exempt (CEF runs with the autoplay policy
 * off), but a plain tab at /overlay/alert isn't — so resume on the first click.
 */
export function primeAudio() {
  const resume = () => void context().resume()
  document.addEventListener('pointerdown', resume, { once: true })
}

function blip(at: number, freq: number, dur: number, vol: number) {
  const ac = context()
  const osc = ac.createOscillator()
  osc.type = 'triangle'
  osc.frequency.setValueAtTime(freq, at)

  const gain = ac.createGain()
  // A step change in gain clicks audibly, so ramp both ends.
  gain.gain.setValueAtTime(0, at)
  gain.gain.linearRampToValueAtTime(vol, at + 0.008)
  gain.gain.setValueAtTime(vol, at + dur * 0.6)
  gain.gain.exponentialRampToValueAtTime(0.0001, at + dur)

  osc.connect(gain).connect(ac.destination)
  osc.start(at)
  osc.stop(at + dur + 0.02)
}

/**
 * Play one of SOUNDS by name. An unknown name falls back to the default rather
 * than throwing — config.json is hand-editable, and a typo there shouldn't take
 * the overlay's alert handler down with it.
 */
export function playSound(name = DEFAULT_SOUND, volume = 0.45) {
  const jingle = SOUNDS[name] ?? LEVELUP
  const ac = context()
  // Scheduling a hair ahead of currentTime keeps the first note from being
  // clipped by the audio thread already being mid-buffer.
  const start = ac.currentTime + 0.02

  jingle.notes.forEach((freq, i) => {
    const last = i === jingle.notes.length - 1
    const at = start + i * jingle.step
    blip(at, freq, last ? jingle.tail : jingle.step, volume)
    if (last && jingle.sparkle) blip(at, freq * 2, jingle.tail, volume * 0.35)
  })
}
