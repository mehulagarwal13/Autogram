import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, popIn } from '../lib/animation';
import { AutogramDashboard } from '../components/AutogramDashboard';
import { processingSteps } from '../lib/data';

const STEP_START = 18;
const STEP_LEN = 34;
const READY_AT = 205;

export const ProcessingScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);
  const listOpacity = fadeOut(frame, READY_AT - 15, 15);
  const readyOpacity = fadeIn(frame, READY_AT, 18);
  const readyScale = popIn(frame, fps, READY_AT);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <AutogramDashboard active="Dashboard">
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          {readyOpacity < 0.99 && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 22, opacity: listOpacity, width: 560 }}>
              {processingSteps.map((step, i) => {
                const start = STEP_START + i * STEP_LEN;
                const doneAt = start + STEP_LEN;
                const opacity = fadeIn(frame, start, 12);
                const isDone = frame >= doneAt;
                const isActive = frame >= start && !isDone;
                const spinAngle = (frame * 10) % 360;
                return (
                  <div
                    key={step}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 16,
                      opacity,
                      fontFamily: theme.font,
                    }}
                  >
                    <div
                      style={{
                        width: 26,
                        height: 26,
                        borderRadius: '50%',
                        flexShrink: 0,
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        background: isDone ? theme.colors.success : 'transparent',
                        border: isDone ? 'none' : `2.5px solid ${theme.colors.borderSoft}`,
                        borderTopColor: isActive ? theme.colors.accent : undefined,
                        transform: isActive ? `rotate(${spinAngle}deg)` : 'none',
                      }}
                    >
                      {isDone && <span style={{ color: '#06231A', fontSize: 14, fontWeight: 900 }}>&#10003;</span>}
                    </div>
                    <span
                      style={{
                        fontSize: 20,
                        fontWeight: isActive || isDone ? 600 : 500,
                        color: isDone ? theme.colors.textSecondary : theme.colors.text,
                      }}
                    >
                      {step}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          {readyOpacity > 0.01 && (
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                gap: 22,
                opacity: readyOpacity,
                transform: `scale(${readyScale})`,
              }}
            >
              <div
                style={{
                  width: 84,
                  height: 84,
                  borderRadius: '50%',
                  background: theme.colors.successSoft,
                  border: `2px solid ${theme.colors.successBorder}`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <span style={{ color: theme.colors.success, fontSize: 40, fontWeight: 900 }}>&#10003;</span>
              </div>
              <div style={{ fontFamily: theme.font, fontSize: 34, fontWeight: 800, color: theme.colors.text }}>
                Application ready
              </div>
            </div>
          )}
        </div>
      </AutogramDashboard>
    </AbsoluteFill>
  );
};
