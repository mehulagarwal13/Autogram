import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, popIn, stagger } from '../lib/animation';
import { AutogramDashboard } from '../components/AutogramDashboard';
import { profileExtractionFields, demoProfile } from '../lib/data';
import { useVideoConfig } from 'remotion';

const UPLOAD_START = 20;
const UPLOAD_END = 80;
const FIELDS_START = 100;
const FIELD_STAGGER = 17;

export const ProfileScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 20);

  const uploadProgress = interpolate(frame, [UPLOAD_START, UPLOAD_END], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const uploadDone = uploadProgress >= 1;
  const checkScale = popIn(frame, fps, UPLOAD_END + 4);

  const h1Opacity = fadeIn(frame, 358, 16) * fadeOut(frame, 400, 14);
  const h2Opacity = fadeIn(frame, 406, 16);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ opacity: sceneOpacity }}>
        <AutogramDashboard active="Profile">
          <div style={{ height: '100%', padding: '46px 56px', boxSizing: 'border-box' }}>
            <div style={{ fontFamily: theme.font, fontSize: 24, fontWeight: 800, color: theme.colors.text, marginBottom: 30 }}>
              Build your profile
            </div>

            <div style={{ display: 'flex', gap: 40, height: '84%' }}>
              <div style={{ width: 360, flexShrink: 0 }}>
                <div
                  style={{
                    borderRadius: theme.radius.lg,
                    border: `1.5px dashed ${uploadDone ? theme.colors.successBorder : theme.colors.border}`,
                    background: theme.colors.surface,
                    padding: '30px 26px',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 16,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <span style={{ fontSize: 22 }}>{'📄'}</span>
                    <span style={{ fontFamily: theme.font, fontSize: 16, fontWeight: 700, color: theme.colors.text }}>
                      {demoProfile.resumeFileName}
                    </span>
                    {uploadDone && (
                      <span
                        style={{
                          marginLeft: 'auto',
                          color: theme.colors.success,
                          fontWeight: 800,
                          fontSize: 18,
                          transform: `scale(${checkScale})`,
                        }}
                      >
                        &#10003;
                      </span>
                    )}
                  </div>
                  <div style={{ height: 6, borderRadius: 3, background: theme.colors.borderSoft, overflow: 'hidden' }}>
                    <div
                      style={{
                        height: '100%',
                        width: `${uploadProgress * 100}%`,
                        background: theme.colors.accentGradient,
                      }}
                    />
                  </div>
                  <div style={{ fontFamily: theme.font, fontSize: 13, color: theme.colors.textMuted }}>
                    {uploadDone ? 'Analyzed by Autogram' : 'Uploading and analyzing…'}
                  </div>
                </div>
              </div>

              <div
                style={{
                  flex: 1,
                  display: 'grid',
                  gridTemplateColumns: '1fr 1fr',
                  gap: 16,
                  alignContent: 'start',
                }}
              >
                {profileExtractionFields.map((field, i) => {
                  const delay = FIELDS_START + stagger(i, 0, FIELD_STAGGER);
                  const opacity = fadeIn(frame, delay, 14);
                  const y = interpolate(frame, [delay, delay + 14], [16, 0], {
                    extrapolateLeft: 'clamp',
                    extrapolateRight: 'clamp',
                  });
                  return (
                    <div
                      key={field.label}
                      style={{
                        opacity,
                        transform: `translateY(${y}px)`,
                        padding: '14px 18px',
                        borderRadius: theme.radius.md,
                        background: theme.colors.surface,
                        border: `1px solid ${theme.colors.borderSoft}`,
                      }}
                    >
                      <div style={{ fontFamily: theme.font, fontSize: 12, color: theme.colors.textMuted, fontWeight: 600, marginBottom: 5 }}>
                        {field.label.toUpperCase()}
                      </div>
                      <div style={{ fontFamily: theme.font, fontSize: 15, color: theme.colors.text, fontWeight: 600 }}>
                        {field.value}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </AutogramDashboard>
      </div>

      <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center' }}>
        {h1Opacity > 0.01 && (
          <div style={{ fontFamily: theme.font, fontSize: 56, fontWeight: 700, color: theme.colors.text, opacity: h1Opacity, textShadow: '0 4px 30px rgba(0,0,0,0.6)' }}>
            Tell Autogram about yourself once.
          </div>
        )}
        {h2Opacity > 0.01 && (
          <div style={{ fontFamily: theme.font, fontSize: 56, fontWeight: 700, color: theme.colors.text, opacity: h2Opacity, textShadow: '0 4px 30px rgba(0,0,0,0.6)' }}>
            Use it everywhere.
          </div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
