import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, typeReveal, clickPulseAny } from '../lib/animation';
import { ChromeWindow } from '../components/ChromeWindow';
import { JobPosting } from '../components/JobPosting';
import { AutogramDashboard } from '../components/AutogramDashboard';
import { Cursor } from '../components/Cursor';
import { Button } from '../components/Button';
import { Notification } from '../components/Notification';
import { demoJob } from '../lib/data';

const TRANSITION_AT = 155;

export const JobScene: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 18);

  // Beat 1: the job posting, on the open web.
  const jobOpacity = fadeOut(frame, TRANSITION_AT, 18);
  const cursorToUrlX = interpolate(frame, [70, 105], [900, 1490], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const cursorToUrlY = interpolate(frame, [70, 105], [560, 42], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const copyClick = clickPulseAny(frame, [108], 10);
  const copiedToastOpacity = fadeIn(frame, 112, 12) * fadeOut(frame, 148, 10);

  // Beat 2: back inside Autogram.
  const dashOpacity = fadeIn(frame, TRANSITION_AT + 15, 20);
  const urlRevealProgress = interpolate(frame, [195, 245], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const urlText = typeReveal(demoJob.url, urlRevealProgress);
  const btnCursorX = interpolate(frame, [255, 290], [560, 980], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const btnCursorY = interpolate(frame, [255, 290], [560, 500], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const btnClick = clickPulseAny(frame, [298], 10);
  const btnPressed = Math.max(0, btnClick);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg }}>
      {frame < TRANSITION_AT + 4 && (
        <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity * jobOpacity }}>
          <ChromeWindow tabs={[{ label: 'Software Engineer — AI Platform', active: true }]} url={demoJob.url} width={1680} height={920}>
            <JobPosting
              title={demoJob.title}
              company={demoJob.company}
              location={demoJob.location}
              description={demoJob.description}
              showApplyButton={false}
            />
            <Cursor x={cursorToUrlX} y={cursorToUrlY} clickProgress={copyClick} />
            {copiedToastOpacity > 0.01 && (
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
                  opacity: copiedToastOpacity,
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
            <div
              style={{
                height: '100%',
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 30,
                position: 'relative',
              }}
            >
              <div style={{ fontFamily: theme.font, fontSize: 15, color: theme.colors.textMuted, fontWeight: 600, letterSpacing: 1 }}>
                JOB URL
              </div>
              <div
                style={{
                  width: 780,
                  padding: '22px 28px',
                  borderRadius: theme.radius.lg,
                  background: theme.colors.surface,
                  border: `1.5px solid ${theme.colors.accentBorder}`,
                  color: theme.colors.text,
                  fontSize: 17,
                  fontFamily: "'SF Mono', 'Consolas', monospace",
                  minHeight: 26,
                }}
              >
                {urlText}
                {urlRevealProgress > 0 && urlRevealProgress < 1 && (
                  <span style={{ display: 'inline-block', width: 2, height: 18, background: theme.colors.accent, marginLeft: 2 }} />
                )}
              </div>
              <div style={{ transform: `scale(${1 - btnPressed * 0.04})` }}>
                <Button label="START APPLICATION" variant="primary" size="lg" glow />
              </div>
              <Cursor x={btnCursorX} y={btnCursorY} clickProgress={btnClick} />
            </div>
          </AutogramDashboard>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
