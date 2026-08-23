import React from 'react';
import { Composition } from 'remotion';
import { AutogramDemo } from './AutogramDemo';
import { FPS, HEIGHT, TOTAL_DURATION, WIDTH } from './lib/timing';
import { AutogramAmericanExpressDemo } from './AutogramAmericanExpressDemo';
import { AMEX_FPS, AMEX_HEIGHT, AMEX_TOTAL_DURATION, AMEX_WIDTH } from './lib/timingAmex';

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="AutogramDemo"
        component={AutogramDemo}
        durationInFrames={TOTAL_DURATION}
        fps={FPS}
        width={WIDTH}
        height={HEIGHT}
      />
      <Composition
        id="AutogramAmericanExpressDemo"
        component={AutogramAmericanExpressDemo}
        durationInFrames={AMEX_TOTAL_DURATION}
        fps={AMEX_FPS}
        width={AMEX_WIDTH}
        height={AMEX_HEIGHT}
      />
    </>
  );
};
