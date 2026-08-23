// Small, reusable animation primitives built on top of Remotion's own
// `interpolate`/`spring`. Scenes pass in their own local frame number
// (relative to their enclosing <Sequence>) plus a delay/duration, so no
// animation timing is hardcoded inside a component — components only ever
// receive an already-computed 0-1 progress value or a pixel offset.

import { interpolate, Easing, spring } from 'remotion';

const clampBoth = { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' } as const;

/** 0 -> 1 opacity ramp starting at `delay`, over `duration` frames. */
export const fadeIn = (frame: number, delay = 0, duration = 15): number =>
  interpolate(frame, [delay, delay + duration], [0, 1], clampBoth);

/** 1 -> 0 opacity ramp starting at `start`, over `duration` frames. */
export const fadeOut = (frame: number, start: number, duration = 15): number =>
  interpolate(frame, [start, start + duration], [1, 0], clampBoth);

/** Vertical offset that eases from `distance`px down to 0 — pair with
 * `transform: translateY(px)`. Reads naturally as "slides up into place". */
export const slideUpPx = (frame: number, delay = 0, duration = 18, distance = 28): number =>
  interpolate(frame, [delay, delay + duration], [distance, 0], {
    ...clampBoth,
    easing: Easing.out(Easing.cubic),
  });

/** Horizontal offset easing from `distance`px to 0. Positive distance slides
 * in from the right, negative from the left. */
export const slideInPx = (frame: number, delay = 0, duration = 18, distance = 40): number =>
  interpolate(frame, [delay, delay + duration], [distance, 0], {
    ...clampBoth,
    easing: Easing.out(Easing.cubic),
  });

/** Generic clamped progress between two frames, 0-1, linear. */
export const progress = (frame: number, start: number, end: number): number =>
  interpolate(frame, [start, end], [0, 1], clampBoth);

/** Eased (ease-in-out) progress between two frames, 0-1. */
export const easedProgress = (frame: number, start: number, end: number): number =>
  interpolate(frame, [start, end], [0, 1], { ...clampBoth, easing: Easing.inOut(Easing.cubic) });

/** A gentle "pop in" scale+fade, driven by a spring rather than a fixed
 * duration — feels less mechanical for cards/badges/buttons appearing. */
export const popIn = (frame: number, fps: number, delay = 0) =>
  spring({
    frame: frame - delay,
    fps,
    config: { damping: 200, mass: 0.6, stiffness: 180 },
  });

/** Frame offset for the Nth item in a staggered list reveal. */
export const stagger = (index: number, baseDelay = 0, gap = 6): number => baseDelay + index * gap;

/** Reveals `text` character-by-character as `progress` goes 0->1 — used for
 * the "typing into a field" effect without re-deriving the math per field. */
export const typeReveal = (text: string, revealProgress: number): string =>
  text.slice(0, Math.round(clamp01(revealProgress) * text.length));

export const clamp01 = (v: number): number => Math.min(1, Math.max(0, v));

/** Cheap deterministic pseudo-random in [0,1), seeded by an integer — used
 * instead of Math.random() so renders stay frame-deterministic. */
export const seeded = (seed: number): number => {
  const x = Math.sin(seed * 999.7) * 43758.5453;
  return x - Math.floor(x);
};

/** A short triangular pulse (0 -> 1 -> 0) centered on frame `at` — used to
 * flash a cursor-click ripple at a precise moment. */
export const clickPulse = (frame: number, at: number, width = 10): number =>
  Math.max(0, 1 - Math.abs(frame - at) / width);

/** The strongest of several click pulses — for a cursor that clicks
 * multiple times across a sequence of waypoints. */
export const clickPulseAny = (frame: number, ats: number[], width = 10): number =>
  ats.reduce((max, at) => Math.max(max, clickPulse(frame, at, width)), 0);
