import React from 'react';
import { AbsoluteFill, Sequence } from 'remotion';
import { theme } from './lib/theme';
import { AMEX_SCENE_DURATIONS, AMEX_SCENE_START } from './lib/timingAmex';
import { Problem } from './scenes/amex/Problem';
import { ProfileSetup } from './scenes/amex/ProfileSetup';
import { JobUrl } from './scenes/amex/JobUrl';
import { Automation } from './scenes/amex/Automation';
import { HumanVerification } from './scenes/amex/HumanVerification';
import { Otp } from './scenes/amex/Otp';
import { Review } from './scenes/amex/Review';
import { Submission } from './scenes/amex/Submission';
import { Tracking } from './scenes/amex/Tracking';
import { Finale } from './scenes/amex/Finale';

/** The Autogram x American Express product-demo video — a second,
 * independent composition. It shares Autogram-brand components (Logo,
 * Button, Cursor, ChromeWindow, AutogramDashboard, GmailWindow, animation
 * helpers) with AutogramDemo.tsx, but every American-Express-specific page
 * lives under src/american-express/ and every scene under src/scenes/amex/
 * — nothing here is imported by, or modifies, the first video. */
export const AutogramAmericanExpressDemo: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: theme.colors.bg }}>
      <Sequence from={AMEX_SCENE_START.problem} durationInFrames={AMEX_SCENE_DURATIONS.problem}>
        <Problem />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.profileSetup} durationInFrames={AMEX_SCENE_DURATIONS.profileSetup}>
        <ProfileSetup />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.jobUrl} durationInFrames={AMEX_SCENE_DURATIONS.jobUrl}>
        <JobUrl />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.automation} durationInFrames={AMEX_SCENE_DURATIONS.automation}>
        <Automation />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.humanVerification} durationInFrames={AMEX_SCENE_DURATIONS.humanVerification}>
        <HumanVerification />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.otp} durationInFrames={AMEX_SCENE_DURATIONS.otp}>
        <Otp />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.review} durationInFrames={AMEX_SCENE_DURATIONS.review}>
        <Review />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.submission} durationInFrames={AMEX_SCENE_DURATIONS.submission}>
        <Submission />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.tracking} durationInFrames={AMEX_SCENE_DURATIONS.tracking}>
        <Tracking />
      </Sequence>
      <Sequence from={AMEX_SCENE_START.finale} durationInFrames={AMEX_SCENE_DURATIONS.finale}>
        <Finale />
      </Sequence>
    </AbsoluteFill>
  );
};
