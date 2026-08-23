import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, typeReveal, clickPulseAny, stagger } from '../../lib/animation';
import { ChromeWindow } from '../../components/ChromeWindow';
import { AmexJobPosting } from '../../american-express/AmexJobPosting';
import { AutogramDashboard } from '../../components/AutogramDashboard';
import { Cursor } from '../../components/Cursor';
import { Button } from '../../components/Button';
import { amexJob } from '../../lib/amexData';

const TRANSITION_AT = 150;
const PROCESSING_START = 270;
const PROCESSING_STEPS = ['Job detected', 'Application platform detected', 'Application form detected', 'Profile matched'];
const OPENING_AT = 390;

export const JobUrl: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 18);

  const jobOpacity = fadeOut(frame, TRANSITION_AT, 16);
  const copyClick = clickPulseAny(frame, [108], 10);
  const cursorX = interpolate(frame, [70, 105], [900, 1490], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const cursorY = interpolate(frame, [70, 105], [560, 42], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const copiedOpacity = fadeIn(frame, 112, 12) * fadeOut(frame, 144, 10);

  const dashOpacity = fadeIn(frame, TRANSITION_AT + 15, 20) * fadeOut(frame, PROCESSING_START - 10, 14);
  const urlProgress = interpolate(frame, [195, 240], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const urlText = typeReveal(amexJob.url, urlProgress);
  const btnClick = clickPulseAny(frame, [255], 10);

  const processingOpacity = fadeIn(frame, PROCESSING_START, 16);
  const openingOpacity = fadeIn(frame, OPENING_AT, 16);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg }}>
      {frame < TRANSITION_AT + 4 && (
        <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity * jobOpacity }}>
          <ChromeWindow tabs={[{ label: 'Software Engineer I | American Express', active: true }]} url={amexJob.url} width={1680} height={920}>
            <AmexJobPosting />
            <Cursor x={cursorX} y={cursorY} clickProgress={copyClick} />
            {copiedOpacity > 0.01 && (
              <div
                style={{
                  position: 'absolute',
                  top: 70,
                  right: 60,
                  fontFamily: theme.font,
                  fontSize: 13,
                  fontWeight: 700,
                  color: '#0A0C10',
                  background: theme.colors.success,
                  padding: '8px 16px',
                  borderRadius: theme.radius.pill,
                  opacity: copiedOpacity,
                }}
              >
                Link copied ✓
              </div>
            )}
          </ChromeWindow>
        </AbsoluteFill>
      )}

      {dashOpacity > 0.01 && (
        <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity * dashOpacity }}>
          <AutogramDashboard active="Dashboard">
            <div style={{ height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 30, position: 'relative' }}>
              <div style={{ fontFamily: theme.font, fontSize: 15, color: theme.colors.textMuted, fontWeight: 600, letterSpacing: 1 }}>JOB URL</div>
              <div
                style={{
                  width: 820,
                  padding: '22px 28px',
                  borderRadius: theme.radius.lg,
                  background: theme.colors.surface,
                  border: `1.5px solid ${theme.colors.accentBorder}`,
                  color: theme.colors.text,
                  fontSize: 16,
                  fontFamily: "'SF Mono','Consolas',monospace",
                  minHeight: 26,
                }}
              >
                {urlText}
                {urlProgress > 0 && urlProgress < 1 && (
                  <span style={{ display: 'inline-block', width: 2, height: 18, background: theme.colors.accent, marginLeft: 2 }} />
                )}
              </div>
              <div style={{ transform: `scale(${1 - Math.max(0, btnClick) * 0.04})` }}>
                <Button label="START APPLICATION" variant="primary" size="lg" glow />
              </div>
              <Cursor x={620} y={555} clickProgress={btnClick} />
            </div>
          </AutogramDashboard>
        </AbsoluteFill>
      )}

      {processingOpacity > 0.01 && (
        <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity * processingOpacity }}>
          <AutogramDashboard active="Dashboard">
            <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              {openingOpacity < 0.99 ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
                  {PROCESSING_STEPS.map((step, i) => {
                    const delay = stagger(i, 0, 30);
                    const opacity = fadeIn(frame - PROCESSING_START, delay, 12);
                    return (
                      <div key={step} style={{ display: 'flex', alignItems: 'center', gap: 14, opacity, fontFamily: theme.font }}>
                        <span style={{ color: theme.colors.success, fontWeight: 800, fontSize: 18 }}>&#10003;</span>
                        <span style={{ fontSize: 20, fontWeight: 600, color: theme.colors.text }}>{step}</span>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <div style={{ fontFamily: theme.font, fontSize: 26, fontWeight: 700, color: theme.colors.textSecondary, opacity: openingOpacity }}>
                  Opening application…
                </div>
              )}
            </div>
          </AutogramDashboard>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
