import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, popIn, stagger } from '../../lib/animation';
import { AutogramDashboard } from '../../components/AutogramDashboard';
import { Button } from '../../components/Button';
import { amexJob, amexProfile } from '../../lib/amexData';

const ROWS = [
  { label: 'Company', value: amexJob.company },
  { label: 'Role', value: amexJob.title },
  { label: 'Resume', value: `${amexProfile.resumeFileName} ✓` },
  { label: 'Personal Information', value: 'Complete ✓' },
  { label: 'Address', value: 'Complete ✓' },
  { label: 'Experience', value: 'Complete ✓' },
  { label: 'Skills', value: 'Complete ✓' },
  { label: 'Application Questions', value: 'Human reviewed ✓' },
  { label: 'Identity Verification', value: 'Complete ✓' },
];

const ROWS_START = 30;
const ROWS_STAGGER = 20;
const READY_AT = 220;
const BUTTON_AT = 255;

export const Review: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);
  const readyOpacity = fadeIn(frame, READY_AT, 16);
  const buttonOpacity = fadeIn(frame, BUTTON_AT, 16);
  const buttonScale = 0.9 + popIn(frame, fps, BUTTON_AT) * 0.1;

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <AutogramDashboard active="Applications">
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ width: 740, padding: '36px 46px', borderRadius: theme.radius.lg, background: theme.colors.surface, border: `1px solid ${theme.colors.borderSoft}` }}>
            <div style={{ fontFamily: theme.font, fontSize: 22, fontWeight: 800, color: theme.colors.text, marginBottom: 22 }}>
              Application Ready
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
              {ROWS.map((row, i) => {
                const opacity = fadeIn(frame, ROWS_START + stagger(i, 0, ROWS_STAGGER), 14);
                return (
                  <div
                    key={row.label}
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '11px 0',
                      borderBottom: i < ROWS.length - 1 ? `1px solid ${theme.colors.borderSoft}` : 'none',
                      opacity,
                    }}
                  >
                    <span style={{ fontFamily: theme.font, fontSize: 14, color: theme.colors.textMuted, fontWeight: 600 }}>{row.label}</span>
                    <span style={{ fontFamily: theme.font, fontSize: 15, color: theme.colors.text, fontWeight: 700 }}>{row.value}</span>
                  </div>
                );
              })}
            </div>

            {readyOpacity > 0.01 && (
              <div style={{ marginTop: 22, textAlign: 'center', fontFamily: theme.font, fontSize: 17, fontWeight: 700, color: theme.colors.success, opacity: readyOpacity }}>
                Ready for final review
              </div>
            )}

            {buttonOpacity > 0.01 && (
              <div style={{ marginTop: 18, display: 'flex', justifyContent: 'center', opacity: buttonOpacity, transform: `scale(${buttonScale})` }}>
                <Button label="CONTINUE TO APPLICATION" variant="primary" glow />
              </div>
            )}
          </div>
        </div>
      </AutogramDashboard>
    </AbsoluteFill>
  );
};
