import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, clickPulseAny } from '../../lib/animation';
import { ChromeWindow } from '../../components/ChromeWindow';
import { Notification } from '../../components/Notification';
import { Button } from '../../components/Button';
import { AmericanExpressQuestions, QuestionBadge } from '../../american-express/AmericanExpressQuestions';
import { amexQuestions } from '../../lib/amexData';

const Q0_DETECTED_AT = 20;
const Q0_SENSITIVE_AT = 60;
const Q0_HUMAN_AT = 100;
const Q0_ANSWER_AT = 175;
const Q0_CONFIRMED_AT = 182;
const Q0_END = 222;

const QUICK_LEN = 37;
const ROW_HEIGHT = 130;

export const HumanVerification: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 16);

  let focusedIndex = 0;
  let badge: QuestionBadge = null;
  const answers: Record<number, 'Yes' | 'No' | undefined> = {};

  if (frame < Q0_END) {
    focusedIndex = 0;
    if (frame < Q0_SENSITIVE_AT) badge = 'detected';
    else if (frame < Q0_HUMAN_AT) badge = 'sensitive';
    else if (frame < Q0_ANSWER_AT) badge = 'human';
    else {
      answers[0] = 'No';
      badge = 'confirmed';
    }
  } else {
    const quickFrame = frame - Q0_END;
    const quickIndex = Math.min(4, Math.floor(quickFrame / QUICK_LEN));
    focusedIndex = 1 + quickIndex;
    const local = quickFrame - quickIndex * QUICK_LEN;
    for (let i = 1; i <= focusedIndex; i++) {
      if (i < focusedIndex) answers[i] = 'No';
    }
    if (local < 16) badge = amexQuestions[focusedIndex].sensitive ? 'sensitive' : 'human';
    else {
      answers[focusedIndex] = 'No';
      badge = 'confirmed';
    }
  }
  // Question 0 stays answered once we've moved on.
  if (frame >= Q0_END) answers[0] = 'No';

  const scrollY = interpolate(focusedIndex, [0, amexQuestions.length - 1], [0, -ROW_HEIGHT * (amexQuestions.length - 2)], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const showHumanPrompt = frame >= Q0_HUMAN_AT - 4 && frame < Q0_ANSWER_AT;
  const notifOpacity = fadeIn(frame, Q0_HUMAN_AT, 14);
  const continuePressed = clickPulseAny(frame, [Q0_ANSWER_AT - 6], 8);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <div style={{ transform: 'scale(0.94)', position: 'relative' }}>
        <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/section/3" width={1680} height={920}>
          <AmericanExpressQuestions focusedIndex={focusedIndex} answers={answers} badge={badge} scrollY={scrollY} />
        </ChromeWindow>

        {showHumanPrompt && (
          <div style={{ position: 'absolute', top: 40, right: -40, display: 'flex', flexDirection: 'column', gap: 16, alignItems: 'flex-end' }}>
            <Notification title="Autogram needs your help." subtitle="This question needs your confirmation before Autogram can continue." variant="warning" appear={notifOpacity} />
            <div style={{ transform: `scale(${1 - continuePressed * 0.05})` }}>
              <Button label="CONTINUE" variant="secondary" />
            </div>
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
};
