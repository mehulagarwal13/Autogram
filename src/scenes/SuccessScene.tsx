import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, popIn, clickPulseAny } from '../lib/animation';
import { Button } from '../components/Button';
import { Cursor } from '../components/Cursor';
import { demoJob } from '../lib/data';

const CLICK_AT = 32;
const BURST_AT = 42;
const TITLE_AT = 70;
const JOB_AT = 108;
const SUB_AT = 140;

export const SuccessScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const buttonsOpacity = fadeOut(frame, BURST_AT - 6, 14);
  const click = clickPulseAny(frame, [CLICK_AT], 8);
  const cursorX = interpolate(frame, [8, CLICK_AT], [420, 560], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });

  const checkScale = popIn(frame, fps, BURST_AT);
  const ring1 = interpolate(frame, [BURST_AT, BURST_AT + 30], [0.6, 2.2], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const ring1Opacity = fadeOut(frame, BURST_AT, 30);
  const ring2 = interpolate(frame, [BURST_AT + 8, BURST_AT + 38], [0.6, 2.6], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const ring2Opacity = fadeOut(frame, BURST_AT + 8, 30);

  const titleOpacity = fadeIn(frame, TITLE_AT, 16);
  const jobOpacity = fadeIn(frame, JOB_AT, 16);
  const subOpacity = fadeIn(frame, SUB_AT, 16);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      {buttonsOpacity > 0.01 && (
        <div style={{ position: 'absolute', display: 'flex', gap: 14, opacity: buttonsOpacity }}>
          <Button label="REVIEW APPLICATION" variant="secondary" />
          <Button label="SUBMIT APPLICATION" variant="primary" glow pressed={click} />
          <Cursor x={cursorX} y={20} clickProgress={click} />
        </div>
      )}

      {frame >= BURST_AT && (
        <div style={{ position: 'absolute', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 26 }}>
          <div style={{ position: 'relative', width: 140, height: 140, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <div
              style={{
                position: 'absolute',
                width: 140,
                height: 140,
                borderRadius: '50%',
                border: `2px solid ${theme.colors.success}`,
                opacity: ring1Opacity * 0.6,
                transform: `scale(${ring1})`,
              }}
            />
            <div
              style={{
                position: 'absolute',
                width: 140,
                height: 140,
                borderRadius: '50%',
                border: `2px solid ${theme.colors.success}`,
                opacity: ring2Opacity * 0.4,
                transform: `scale(${ring2})`,
              }}
            />
            <div
              style={{
                width: 108,
                height: 108,
                borderRadius: '50%',
                background: theme.colors.success,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                transform: `scale(${checkScale})`,
                boxShadow: '0 0 60px rgba(62,207,142,0.5)',
              }}
            >
              <span style={{ color: '#06231A', fontSize: 52, fontWeight: 900 }}>&#10003;</span>
            </div>
          </div>

          {titleOpacity > 0.01 && (
            <div style={{ fontFamily: theme.font, fontSize: 46, fontWeight: 800, color: theme.colors.text, opacity: titleOpacity }}>
              Application Submitted
            </div>
          )}
          {jobOpacity > 0.01 && (
            <div style={{ fontFamily: theme.font, fontSize: 24, fontWeight: 700, color: theme.colors.textSecondary, opacity: jobOpacity }}>
              {demoJob.title}
            </div>
          )}
          {subOpacity > 0.01 && (
            <div style={{ fontFamily: theme.font, fontSize: 18, fontWeight: 500, color: theme.colors.success, opacity: subOpacity }}>
              Successfully submitted
            </div>
          )}
        </div>
      )}
    </AbsoluteFill>
  );
};
