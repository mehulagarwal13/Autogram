import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, popIn, stagger } from '../lib/animation';
import { AutogramDashboard } from '../components/AutogramDashboard';
import { Button } from '../components/Button';
import { demoJob, demoProfile } from '../lib/data';

const ROWS = [
  { label: 'Role', value: demoJob.title },
  { label: 'Company', value: demoJob.company },
  { label: 'Resume', value: `${demoProfile.resumeFileName} ✓` },
  { label: 'Profile', value: 'Complete ✓' },
  { label: 'Required Questions', value: 'Complete ✓' },
  { label: 'Human Verification', value: 'Complete ✓' },
];

const ROWS_START = 30;
const ROWS_STAGGER = 22;
const READY_AT = 210;
const BUTTONS_AT = 245;

export const ReviewScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);
  const readyOpacity = fadeIn(frame, READY_AT, 16);
  const buttonsOpacity = fadeIn(frame, BUTTONS_AT, 16);
  const buttonsScale = 0.9 + popIn(frame, fps, BUTTONS_AT) * 0.1;

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <AutogramDashboard active="Applications">
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div
            style={{
              width: 720,
              padding: '40px 46px',
              borderRadius: theme.radius.lg,
              background: theme.colors.surface,
              border: `1px solid ${theme.colors.borderSoft}`,
            }}
          >
            <div style={{ fontFamily: theme.font, fontSize: 22, fontWeight: 800, color: theme.colors.text, marginBottom: 26 }}>
              Application Review
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              {ROWS.map((row, i) => {
                const opacity = fadeIn(frame, ROWS_START + stagger(i, 0, ROWS_STAGGER), 14);
                return (
                  <div
                    key={row.label}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '13px 0',
                      borderBottom: i < ROWS.length - 1 ? `1px solid ${theme.colors.borderSoft}` : 'none',
                      opacity,
                    }}
                  >
                    <span style={{ fontFamily: theme.font, fontSize: 15, color: theme.colors.textMuted, fontWeight: 600 }}>
                      {row.label}
                    </span>
                    <span style={{ fontFamily: theme.font, fontSize: 16, color: theme.colors.text, fontWeight: 700 }}>
                      {row.value}
                    </span>
                  </div>
                );
              })}
            </div>

            {readyOpacity > 0.01 && (
              <div
                style={{
                  marginTop: 26,
                  textAlign: 'center',
                  fontFamily: theme.font,
                  fontSize: 18,
                  fontWeight: 700,
                  color: theme.colors.success,
                  opacity: readyOpacity,
                }}
              >
                Ready to submit
              </div>
            )}

            {buttonsOpacity > 0.01 && (
              <div
                style={{
                  marginTop: 22,
                  display: 'flex',
                  gap: 14,
                  justifyContent: 'center',
                  opacity: buttonsOpacity,
                  transform: `scale(${buttonsScale})`,
                }}
              >
                <Button label="REVIEW APPLICATION" variant="secondary" />
                <Button label="SUBMIT APPLICATION" variant="primary" glow />
              </div>
            )}
          </div>
        </div>
      </AutogramDashboard>
    </AbsoluteFill>
  );
};
