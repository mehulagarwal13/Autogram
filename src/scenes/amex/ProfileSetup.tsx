import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, popIn, stagger } from '../../lib/animation';
import { AutogramDashboard } from '../../components/AutogramDashboard';
import { amexProfile } from '../../lib/amexData';

const UPLOAD_START = 20;
const UPLOAD_END = 70;
const EXTRACTING_AT = 78;
const CHECKLIST_START = 102;
const CHECKLIST_STAGGER = 30;
const READY_AT = 270;
const CAPTION1_AT = 312;
const CAPTION2_AT = 348;

const CHECKLIST = ['Personal Information', 'Education', 'Experience', 'Skills', 'Resume'];

export const ProfileSetup: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const uploadProgress = interpolate(frame, [UPLOAD_START, UPLOAD_END], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const extractingOpacity = fadeIn(frame, EXTRACTING_AT, 12) * fadeOut(frame, CHECKLIST_START - 6, 12);
  const readyOpacity = fadeIn(frame, READY_AT, 16);
  const readyScale = popIn(frame, fps, READY_AT);
  const caption1Opacity = fadeIn(frame, CAPTION1_AT, 14) * fadeOut(frame, CAPTION2_AT - 8, 12);
  const caption2Opacity = fadeIn(frame, CAPTION2_AT, 14);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <AutogramDashboard active="Profile">
        <div style={{ height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 28, width: 480 }}>
            <div
              style={{
                width: '100%',
                borderRadius: theme.radius.lg,
                border: `1.5px dashed ${uploadProgress >= 1 ? theme.colors.successBorder : theme.colors.border}`,
                background: theme.colors.surface,
                padding: '26px 24px',
                display: 'flex',
                flexDirection: 'column',
                gap: 14,
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span style={{ fontSize: 20 }}>{'📄'}</span>
                <span style={{ fontFamily: theme.font, fontSize: 15, fontWeight: 700, color: theme.colors.text }}>
                  {amexProfile.resumeFileName}
                </span>
                {uploadProgress >= 1 && <span style={{ marginLeft: 'auto', color: theme.colors.success, fontWeight: 800 }}>&#10003;</span>}
              </div>
              <div style={{ height: 6, borderRadius: 3, background: theme.colors.borderSoft, overflow: 'hidden' }}>
                <div style={{ height: '100%', width: `${uploadProgress * 100}%`, background: theme.colors.accentGradient }} />
              </div>
            </div>

            {extractingOpacity > 0.01 && (
              <div style={{ fontFamily: theme.font, fontSize: 16, color: theme.colors.textSecondary, opacity: extractingOpacity }}>
                Extracting profile…
              </div>
            )}

            {frame >= CHECKLIST_START && readyOpacity < 0.99 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 14, width: '100%' }}>
                {CHECKLIST.map((item, i) => {
                  const delay = CHECKLIST_START + stagger(i, 0, CHECKLIST_STAGGER);
                  const opacity = fadeIn(frame, delay, 14);
                  return (
                    <div key={item} style={{ display: 'flex', alignItems: 'center', gap: 12, opacity, fontFamily: theme.font }}>
                      <span style={{ color: theme.colors.success, fontWeight: 800, fontSize: 16 }}>&#10003;</span>
                      <span style={{ fontSize: 17, fontWeight: 600, color: theme.colors.text }}>{item}</span>
                    </div>
                  );
                })}
              </div>
            )}

            {readyOpacity > 0.01 && (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16, opacity: readyOpacity, transform: `scale(${readyScale})` }}>
                <div
                  style={{
                    width: 72,
                    height: 72,
                    borderRadius: '50%',
                    background: theme.colors.successSoft,
                    border: `2px solid ${theme.colors.successBorder}`,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <span style={{ color: theme.colors.success, fontSize: 34, fontWeight: 900 }}>&#10003;</span>
                </div>
                <div style={{ fontFamily: theme.font, fontSize: 26, fontWeight: 800, color: theme.colors.text }}>Profile ready</div>
              </div>
            )}
          </div>
        </div>
      </AutogramDashboard>

      <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center' }}>
        {caption1Opacity > 0.01 && (
          <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 50, fontWeight: 700, color: theme.colors.text, opacity: caption1Opacity }}>
            &quot;Set it up once.&quot;
          </div>
        )}
        {caption2Opacity > 0.01 && (
          <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 50, fontWeight: 700, color: theme.colors.text, opacity: caption2Opacity }}>
            &quot;Use it across applications.&quot;
          </div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
