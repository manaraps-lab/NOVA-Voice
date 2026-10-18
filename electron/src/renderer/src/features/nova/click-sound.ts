/**
 * Click and status sounds, synthesised with Web Audio so the app ships no
 * audio files. Everything is wrapped: a missing AudioContext or blocked
 * storage must never break the interface.
 */
const KEY = 'nova.sound.v1';
let ctx: AudioContext | null = null;
let muted = false;

try {
  muted = localStorage.getItem(KEY) === 'off';
} catch {
  muted = false;
}

function context(): AudioContext | null {
  try {
    if (!ctx) {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
      if (!Ctor) return null;
      ctx = new Ctor();
    }
    if (ctx.state === 'suspended') void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

function tone(freq: number, duration: number, opts: { type?: OscillatorType; gain?: number; at?: number; to?: number } = {}) {
  if (muted) return;
  const c = context();
  if (!c) return;
  try {
    const t0 = c.currentTime + (opts.at ?? 0);
    const osc = c.createOscillator();
    const g = c.createGain();
    osc.type = opts.type ?? 'sine';
    osc.frequency.setValueAtTime(freq, t0);
    if (opts.to) osc.frequency.exponentialRampToValueAtTime(opts.to, t0 + duration);
    const peak = opts.gain ?? 0.06;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
    osc.connect(g).connect(c.destination);
    osc.start(t0);
    osc.stop(t0 + duration + 0.02);
  } catch {
    // Sound is decoration.
  }
}

export const sfx = {
  click: () => tone(1500, 0.05, { type: 'triangle', to: 900, gain: 0.05 }),
  select: () => tone(880, 0.07, { type: 'sine', gain: 0.05 }),
  start: () => {
    tone(523, 0.12, { gain: 0.06 });
    tone(784, 0.16, { gain: 0.06, at: 0.1 });
  },
  pause: () => tone(660, 0.09, { type: 'triangle', to: 440, gain: 0.05 }),
  resume: () => tone(440, 0.09, { type: 'triangle', to: 660, gain: 0.05 }),
  cancel: () => tone(330, 0.14, { type: 'triangle', to: 200, gain: 0.06 }),
  success: () => {
    tone(659, 0.14, { gain: 0.06 });
    tone(880, 0.14, { gain: 0.06, at: 0.12 });
    tone(1175, 0.24, { gain: 0.06, at: 0.24 });
  },
  error: () => {
    tone(220, 0.18, { type: 'sawtooth', gain: 0.04 });
    tone(165, 0.24, { type: 'sawtooth', gain: 0.04, at: 0.16 });
  },
  isMuted: () => muted,
  setMuted(value: boolean) {
    muted = value;
    try {
      localStorage.setItem(KEY, value ? 'off' : 'on');
    } catch {
      // Keeps the choice for this session only.
    }
  },
};
