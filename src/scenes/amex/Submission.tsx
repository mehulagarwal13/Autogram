import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, popIn, clickPulseAny } from '../../lib/animation';
import { amexTheme } from '../../lib/amexTheme';
import { ChromeWindow } from '../../components/ChromeWindow';
import { Cursor } from '../../components/Cursor';
import { AmexAppShell } from '../../american-express/AmexAppShell';
import { amexJob } from '../../lib/amexData';

const CLICK_AT = 60;
const SUBMITTING_END = 130;
const BURST_AT = 145;
const TITLE_AT = 175;

export const Submission: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const amexOpacity = fadeOut(frame, BURST_AT - 10, 14);
  const click = clickPulseAny(frame, [CLICK_AT], 8);
  const submittingOpacity = fadeIn(frame, CLICK_AT + 4, 10) * fadeOut(frame, SUBMITTING_END - 8, 10);

  const checkScale = popIn(frame, fps, BURST_AT);
  const ring1 = interpolate(frame, [BURST_AT, BURST_AT + 30], [0.6, 2.2], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const ring1Opacity = fadeOut(frame, BURST_AT, 30);
  const titleOpacity = fadeIn(frame, TITLE_AT, 16);
  const subOpacity = fadeIn(frame, TITLE_AT + 30, 16);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      {amexOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: amexOpacity, transform: 'scale(0.94)' }}>
          <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/review" width={1680} height={920}>
            <AmexAppShell jobTitle={amexJob.title} variant="plain">
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', gap: 26 }}>
                <div style={{ fontFamily: amexTheme.headingFont, fontSize: 30, color: amexTheme.colors.blueDark }}>Review and Submit</div>
                <div style={{ fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.body, maxWidth: 520, textAlign: 'center', lineHeight: 1.6 }}>
                  Your application for {amexJob.title} is complete. Submit when you&apos;re ready.
                </div>
                <div
                  style={{
                    padding: '15px 46px',
                    borderRadius: 8,
                    background: amexTheme.colors.blue,
                    color: '#FFFFFF',
                    fontFamily: amexTheme.font,
                    fontSize: 16,
                    fontWeight: 700,
                    transform: `scale(${1 - Math.max(0, click) * 0.05})`,
                  }}
                >
                  Submit Application
                </div>
                {submittingOpacity > 0.01 && (
                  <div style={{ opacity: submittingOpacity, fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.muted }}>
                    Submitting application…
                  </div>
                )}
              </div>
            </AmexAppShell>
            <Cursor x={960} y={620} clickProgress={click} />
          </ChromeWindow>
        </div>
      )}

      {frame >= BURST_AT && (
        <div style={{ position: 'absolute', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 22 }}>
          <div style={{ position: 'relative', width: 130, height: 130, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <div
              style={{
                position: 'absolute',
                width: 130,
                height: 130,
                borderRadius: '50%',
                border: `2px solid ${theme.colors.success}`,
                opacity: ring1Opacity * 0.6,
                transform: `scale(${ring1})`,
              }}
            />
            <div
              style={{
                width: 100,
                height: 100,
                borderRadius: '50%',
                background: theme.colors.success,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                transform: `scale(${checkScale})`,
                boxShadow: '0 0 60px rgba(62,207,142,0.5)',
              }}
            >
              <span style={{ color: '#06231A', fontSize: 48, fontWeight: 900 }}>&#10003;</span>
            </div>
          </div>

          {titleOpacity > 0.01 && (
            <div style={{ fontFamily: theme.font, fontSize: 42, fontWeight: 800, color: theme.colors.text, opacity: titleOpacity }}>
              Application submitted
            </div>
          )}
          {subOpacity > 0.01 && (
            <div style={{ fontFamily: theme.font, fontSize: 20, fontWeight: 600, color: theme.colors.textSecondary, opacity: subOpacity }}>
              {amexJob.title} · {amexJob.company}
            </div>
          )}
        </div>
      )}
    </AbsoluteFill>
  );
};
