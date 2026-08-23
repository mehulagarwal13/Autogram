import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut } from '../lib/animation';
import { ChromeWindow } from '../components/ChromeWindow';
import { ApplicationForm } from '../components/ApplicationForm';
import { screeningQuestions } from '../lib/data';

const QUESTIONS_START = 20;
const SEG_LEN = 100;
const HEADLINE_AT = QUESTIONS_START + screeningQuestions.length * SEG_LEN + 10;

export const QuestionsScene: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 18);

  const montageFrame = frame - QUESTIONS_START;
  const rawIndex = Math.floor(montageFrame / SEG_LEN);
  const segIndex = Math.max(0, Math.min(screeningQuestions.length - 1, rawIndex));
  const segFrame = Math.max(0, montageFrame - segIndex * SEG_LEN);
  const q = screeningQuestions[segIndex];

  const segCrossfade =
    fadeIn(segFrame, 0, 14) *
    fadeOut(segFrame, SEG_LEN - 16, 14);

  const montageOpacity = fadeOut(frame, HEADLINE_AT - 12, 16);
  const headlineOpacity1 = fadeIn(frame, HEADLINE_AT, 16) * fadeOut(frame, HEADLINE_AT + 60, 14);
  const headlineOpacity2 = fadeIn(frame, HEADLINE_AT + 78, 16);

  const revealed = interpolate(segFrame, [8, 32], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      {montageOpacity > 0.01 && (
        <div style={{ opacity: montageOpacity, transform: 'scale(0.94)' }}>
          <ChromeWindow tabs={[{ label: 'Application questions', active: true }]} url="careers.exampletech.com/apply/screening" width={1680} height={920}>
            {/* The browser chrome and page background stay fully opaque — only
                the question content itself crossfades, so switching questions
                reads as a content swap rather than the whole page dimming. */}
            <div style={{ opacity: segCrossfade, height: '100%' }}>
              <ApplicationForm
                title="A few more questions"
                fields={[
                  {
                    label: q.question,
                    value: q.answer,
                    revealed,
                    kind: 'question',
                    sourceTag: q.source,
                  },
                ]}
              />
            </div>
          </ChromeWindow>
        </div>
      )}

      {headlineOpacity1 > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 60, fontWeight: 700, color: theme.colors.text, opacity: headlineOpacity1 }}>
          &quot;Autogram doesn&apos;t just fill forms.&quot;
        </div>
      )}
      {headlineOpacity2 > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 60, fontWeight: 700, color: theme.colors.text, opacity: headlineOpacity2 }}>
          &quot;It understands them.&quot;
        </div>
      )}
    </AbsoluteFill>
  );
};
