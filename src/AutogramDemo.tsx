import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';
import { theme } from './lib/theme';
import { SCENE_DURATIONS, SCENE_START } from './lib/timing';
import { ProblemScene } from './scenes/ProblemScene';
import { IntroScene } from './scenes/IntroScene';
import { ProfileScene } from './scenes/ProfileScene';
import { JobScene } from './scenes/JobScene';
import { ProcessingScene } from './scenes/ProcessingScene';
import { AutomationScene } from './scenes/AutomationScene';
import { QuestionsScene } from './scenes/QuestionsScene';
import { CaptchaScene } from './scenes/CaptchaScene';
import { OtpScene } from './scenes/OtpScene';
import { ReviewScene } from './scenes/ReviewScene';
import { SuccessScene } from './scenes/SuccessScene';
import { TrackingScene } from './scenes/TrackingScene';
import { VisionScene } from './scenes/VisionScene';
import { FinalScene } from './scenes/FinalScene';

/** The full Autogram product-demo video, one <Sequence> per scene. Each
 * scene component owns its own internal timing (relative to its Sequence),
 * so this file is purely "which scene, for how long, in what order" — see
 * `lib/timing.ts` for the actual durations. */
export const AutogramDemo: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: theme.colors.bg }}>
      <Sequence from={SCENE_START.problem} durationInFrames={SCENE_DURATIONS.problem}>
        <ProblemScene />
      </Sequence>
      <Sequence from={SCENE_START.intro} durationInFrames={SCENE_DURATIONS.intro}>
        <IntroScene />
      </Sequence>
      <Sequence from={SCENE_START.profile} durationInFrames={SCENE_DURATIONS.profile}>
        <ProfileScene />
      </Sequence>
      <Sequence from={SCENE_START.job} durationInFrames={SCENE_DURATIONS.job}>
        <JobScene />
      </Sequence>
      <Sequence from={SCENE_START.processing} durationInFrames={SCENE_DURATIONS.processing}>
        <ProcessingScene />
      </Sequence>
      <Sequence from={SCENE_START.automation} durationInFrames={SCENE_DURATIONS.automation}>
        <AutomationScene />
      </Sequence>
      <Sequence from={SCENE_START.questions} durationInFrames={SCENE_DURATIONS.questions}>
        <QuestionsScene />
      </Sequence>
      <Sequence from={SCENE_START.captcha} durationInFrames={SCENE_DURATIONS.captcha}>
        <CaptchaScene />
      </Sequence>
      <Sequence from={SCENE_START.otp} durationInFrames={SCENE_DURATIONS.otp}>
        <OtpScene />
      </Sequence>
      <Sequence from={SCENE_START.review} durationInFrames={SCENE_DURATIONS.review}>
        <ReviewScene />
      </Sequence>
      <Sequence from={SCENE_START.success} durationInFrames={SCENE_DURATIONS.success}>
        <SuccessScene />
      </Sequence>
      <Sequence from={SCENE_START.tracking} durationInFrames={SCENE_DURATIONS.tracking}>
        <TrackingScene />
      </Sequence>
      <Sequence from={SCENE_START.vision} durationInFrames={SCENE_DURATIONS.vision}>
        <VisionScene />
      </Sequence>
      <Sequence from={SCENE_START.final} durationInFrames={SCENE_DURATIONS.final}>
        <FinalScene />
      </Sequence>
    </AbsoluteFill>
  );
};
