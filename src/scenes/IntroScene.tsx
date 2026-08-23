import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, popIn } from '../lib/animation';
import { Logo } from '../components/Logo';
import { AutogramDashboard } from '../components/AutogramDashboard';

export const IntroScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const logoBeatOpacity = fadeIn(frame, 0, 20) * fadeOut(frame, 95, 20);
  const logoScale = popIn(frame, fps, 4);
  const taglineOpacity = fadeIn(frame, 30, 20) * fadeOut(frame, 95, 20);

  const dashOpacity = fadeIn(frame, 115, 22);
  const dashScale = 0.94 + popIn(frame, fps, 115) * 0.06;
  const captionOpacity = fadeIn(frame, 175, 20);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center' }}>
      {logoBeatOpacity > 0.01 && (
        <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', gap: 26 }}>
          <div
            style={{
              opacity: logoBeatOpacity,
              transform: `scale(${logoScale})`,
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 26,
            }}
          >
            <Logo size={78} />
            <div
              style={{
                fontFamily: theme.font,
                fontSize: 30,
                fontWeight: 500,
                color: theme.colors.textSecondary,
                opacity: taglineOpacity,
              }}
            >
              Your AI job application agent.
            </div>
          </div>
        </AbsoluteFill>
      )}

      {dashOpacity > 0.01 && (
        <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', gap: 40 }}>
          <div style={{ opacity: dashOpacity, transform: `scale(${dashScale})` }}>
            <AutogramDashboard active="Dashboard">
              <div
                style={{
                  height: '100%',
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 28,
                }}
              >
                <div style={{ fontFamily: theme.font, fontSize: 15, color: theme.colors.textMuted, fontWeight: 600, letterSpacing: 1 }}>
                  START A NEW APPLICATION
                </div>
                <div
                  style={{
                    width: 640,
                    padding: '22px 28px',
                    borderRadius: theme.radius.lg,
                    background: theme.colors.surface,
                    border: `1.5px solid ${theme.colors.border}`,
                    color: theme.colors.textMuted,
                    fontSize: 17,
                    fontFamily: theme.font,
                  }}
                >
                  Paste a job link…
                </div>
              </div>
            </AutogramDashboard>
          </div>
          <div
            style={{
              fontFamily: theme.font,
              fontSize: 34,
              fontWeight: 700,
              color: theme.colors.text,
              opacity: captionOpacity,
              textAlign: 'center',
            }}
          >
            Paste a job link. Let Autogram do the rest.
          </div>
        </AbsoluteFill>
      )}
    </AbsoluteFill>
  );
};
