// A synthesised "level up" jingle: an ascending triangle-wave arpeggio, in the
// spirit of the pixel font the alert card uses. Synthesised rather than bundled
// as an audio file so there's no binary asset to ship and the notes stay
// tweakable right here.

let ctx: AudioContext | null = null

// C5, E5, G5, C6 — a major triad resolving up an octave.
const NOTES = [523.25, 659.25, 783.99, 1046.5]
const BLIP_S = 0.075 // each lead-in note
const FINAL_S = 0.34 // the last note rings out instead

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

export function playLevelUp(volume = 0.45) {
  const ac = context()
  // Scheduling a hair ahead of currentTime keeps the first note from being
  // clipped by the audio thread already being mid-buffer.
  const start = ac.currentTime + 0.02

  NOTES.forEach((freq, i) => {
    const last = i === NOTES.length - 1
    const at = start + i * BLIP_S
    blip(at, freq, last ? FINAL_S : BLIP_S, volume)
    // Octave sparkle on the final note only — it's what makes the phrase land
    // as an arrival rather than just a fourth blip.
    if (last) blip(at, freq * 2, FINAL_S, volume * 0.35)
  })
}
