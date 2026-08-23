// Per-scene durations (in frames, at FPS below) — the single source of
// truth for both AutogramDemo.tsx's <Sequence> placement and any scene that
// needs to know its own total length (e.g. to time an exit fade).

export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;

export const SCENE_DURATIONS = {
  problem: 600,
  intro: 300,
  profile: 450,
  job: 360,
  processing: 240,
  automation: 480,
  questions: 420,
  captcha: 360,
  otp: 420,
  review: 300,
  success: 240,
  tracking: 300,
  vision: 360,
  final: 300,
} as const;

export type SceneKey = keyof typeof SCENE_DURATIONS;

export const SCENE_ORDER: SceneKey[] = [
  'problem',
  'intro',
  'profile',
  'job',
  'processing',
  'automation',
  'questions',
  'captcha',
  'otp',
  'review',
  'success',
  'tracking',
  'vision',
  'final',
];

export const TOTAL_DURATION = SCENE_ORDER.reduce((sum, key) => sum + SCENE_DURATIONS[key], 0);

/** Frame at which each scene starts, in composition-absolute frames. */
export const SCENE_START: Record<SceneKey, number> = (() => {
  let cursor = 0;
  const result = {} as Record<SceneKey, number>;
  for (const key of SCENE_ORDER) {
    result[key] = cursor;
    cursor += SCENE_DURATIONS[key];
  }
  return result;
})();
