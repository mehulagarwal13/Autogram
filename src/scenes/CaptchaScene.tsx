import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, clickPulseAny, popIn } from '../lib/animation';
import { useVideoConfig } from 'remotion';
import { ChromeWindow } from '../components/ChromeWindow';
import { Cursor } from '../components/Cursor';
import { Notification } from '../components/Notification';
import { Button } from '../components/Button';
import { ProgressBar } from '../components/ProgressBar';

const DETECTED_AT = 18;
const PAUSED_AT = 60;
const NOTIF_AT = 110;
const HUMAN_ACT_AT = 175;
const CHECK_AT = 235;
const RESUME_AT = 280;

export const CaptchaScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const detectedOpacity = fadeIn(frame, DETECTED_AT, 14) * fadeOut(frame, PAUSED_AT + 40, 14);
  const pausedOpacity = fadeIn(frame, PAUSED_AT, 14);
  const notifOpacity = interpolate(frame, [NOTIF_AT, NOTIF_AT + 16], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const continuePressed = clickPulseAny(frame, [HUMAN_ACT_AT - 6], 8);

  const humanClick = clickPulseAny(frame, [CHECK_AT - 10], 10);
  const checked = frame >= CHECK_AT;
  const checkScale = popIn(frame, fps, CHECK_AT);

  const verifiedOpacity = fadeIn(frame, CHECK_AT + 10, 14);
  const resumeOpacity = fadeIn(frame, RESUME_AT, 14);
  const resumeProgress = interpolate(frame, [RESUME_AT + 10, 355], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <div style={{ transform: 'scale(0.94)', position: 'relative' }}>
        <ChromeWindow tabs={[{ label: 'Software Engineer — AI Platform', active: true }]} url="careers.exampletech.com/apply/verify" width={1680} height={920}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', flexDirection: 'column', gap: 24 }}>
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 16,
                padding: '24px 38px',
                borderRadius: 12,
                border: `1.5px solid ${checked ? '#B9E8CE' : '#D7DAE0'}`,
                background: checked ? '#F0FBF5' : '#FAFBFC',
              }}
            >
              <div
                style={{
                  width: 30,
                  height: 30,
                  borderRadius: 6,
                  border: '2px solid #B7BCC8',
                  background: checked ? '#3ECF8E' : '#FFFFFF',
                  transform: `scale(${checked ? checkScale : 1})`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  fontWeight: 800,
                  color: '#06231A',
                }}
              >
                {checked ? '✓' : ''}
              </div>
              <span style={{ fontSize: 18, color: '#22252E', fontWeight: 600 }}>I&apos;m not a robot</span>
            </div>
            {frame >= HUMAN_ACT_AT && frame < CHECK_AT + 20 && (
              <div style={{ fontSize: 13, color: '#8A90A0', fontFamily: theme.font, fontWeight: 600 }}>
                Completed by you
              </div>
            )}
          </div>
          {frame >= HUMAN_ACT_AT && frame < CHECK_AT + 8 && <Cursor x={870} y={470} clickProgress={humanClick} />}
        </ChromeWindow>

        {detectedOpacity > 0.01 && (
          <div
            style={{
              position: 'absolute',
              top: -30,
              left: '50%',
              transform: 'translateX(-50%)',
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 10,
              opacity: detectedOpacity,
            }}
          >
            <div
              style={{
                fontFamily: theme.font,
                fontSize: 15,
                fontWeight: 800,
                letterSpacing: 1.5,
                color: theme.colors.warning,
                background: theme.colors.warningSoft,
                border: `1px solid ${theme.colors.warningBorder}`,
                padding: '8px 20px',
                borderRadius: theme.radius.pill,
              }}
            >
              CAPTCHA DETECTED
            </div>
          </div>
        )}

        {pausedOpacity > 0.01 && frame < CHECK_AT && (
          <div
            style={{
              position: 'absolute',
              bottom: -56,
              left: '50%',
              transform: 'translateX(-50%)',
              opacity: pausedOpacity,
              fontFamily: theme.font,
              fontSize: 20,
              fontWeight: 600,
              color: theme.colors.textSecondary,
            }}
          >
            Human verification required — Autogram has paused.
          </div>
        )}

        {frame >= CHECK_AT && verifiedOpacity > 0.01 && (
          <div
            style={{
              position: 'absolute',
              bottom: -56,
              left: '50%',
              transform: 'translateX(-50%)',
              opacity: verifiedOpacity,
              display: 'flex',
              alignItems: 'center',
              gap: 10,
              fontFamily: theme.font,
              fontSize: 20,
              fontWeight: 700,
              color: theme.colors.success,
            }}
          >
            <span>&#10003;</span>
            <span>Verification complete</span>
          </div>
        )}
      </div>

      {notifOpacity > 0.01 && frame < HUMAN_ACT_AT + 10 && (
        <div style={{ position: 'absolute', top: 90, right: 90, display: 'flex', flexDirection: 'column', gap: 16, alignItems: 'flex-end' }}>
          <Notification title="Autogram needs your help." subtitle="A CAPTCHA appeared — complete it and Autogram will continue." variant="warning" appear={notifOpacity} />
          <div style={{ opacity: notifOpacity, transform: `scale(${1 - continuePressed * 0.05})` }}>
            <Button label="CONTINUE" variant="secondary" />
          </div>
        </div>
      )}

      {resumeOpacity > 0.01 && (
        <div style={{ position: 'absolute', bottom: 60, opacity: resumeOpacity }}>
          <ProgressBar progress={resumeProgress} label="Autogram is resuming automatically…" width={420} color={theme.colors.success} />
        </div>
      )}
    </AbsoluteFill>
  );
};
