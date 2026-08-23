// Per-scene durations for AutogramAmericanExpressDemo — kept entirely
// separate from lib/timing.ts (the first video's timings) so neither
// composition can accidentally affect the other's pacing.

export const AMEX_FPS = 30;
export const AMEX_WIDTH = 1920;
export const AMEX_HEIGHT = 1080;

export const AMEX_SCENE_DURATIONS = {
  problem: 480,
  profileSetup: 380,
  jobUrl: 420,
  automation: 1240,
  humanVerification: 420,
  otp: 420,
  review: 340,
  submission: 260,
  tracking: 300,
  finale: 300,
} as const;

export type AmexSceneKey = keyof typeof AMEX_SCENE_DURATIONS;

export const AMEX_SCENE_ORDER: AmexSceneKey[] = [
  'problem',
  'profileSetup',
  'jobUrl',
  'automation',
  'humanVerification',
  'otp',
  'review',
  'submission',
  'tracking',
  'finale',
];

export const AMEX_TOTAL_DURATION = AMEX_SCENE_ORDER.reduce((sum, key) => sum + AMEX_SCENE_DURATIONS[key], 0);

export const AMEX_SCENE_START: Record<AmexSceneKey, number> = (() => {
  let cursor = 0;
  const result = {} as Record<AmexSceneKey, number>;
  for (const key of AMEX_SCENE_ORDER) {
    result[key] = cursor;
    cursor += AMEX_SCENE_DURATIONS[key];
  }
  return result;
})();
