import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, popIn } from '../../lib/animation';
import { Logo } from '../../components/Logo';

const LINE1_AT = 20;
const LINE2_AT = 85;
const LINE3_AT = 150;
const BRAND_AT = 215;
const CTA_AT = 250;
const LOGO_AT = 278;

export const Finale: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const l1 = fadeIn(frame, LINE1_AT, 14) * fadeOut(frame, LINE2_AT - 10, 12);
  const l2 = fadeIn(frame, LINE2_AT, 14) * fadeOut(frame, LINE3_AT - 10, 12);
  const l3 = fadeIn(frame, LINE3_AT, 14) * fadeOut(frame, BRAND_AT - 10, 12);
  const brand = fadeIn(frame, BRAND_AT, 14) * fadeOut(frame, CTA_AT - 8, 12);
  const cta = fadeIn(frame, CTA_AT, 14) * fadeOut(frame, LOGO_AT - 6, 10);
  const finalOpacity = fadeIn(frame, LOGO_AT, 18);
  const finalScale = 0.85 + popIn(frame, fps, LOGO_AT) * 0.15;

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at 50% 40%, ${theme.colors.bgGradientEnd} 0%, ${theme.colors.bg} 70%)`,
        alignItems: 'center',
        justifyContent: 'center',
        opacity: sceneOpacity,
      }}
    >
      {l1 > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 52, fontWeight: 700, color: theme.colors.text, opacity: l1 }}>
          You find the job.
        </div>
      )}
      {l2 > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 52, fontWeight: 700, color: theme.colors.text, opacity: l2, textAlign: 'center' }}>
          Autogram handles the repetitive work.
        </div>
      )}
      {l3 > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 52, fontWeight: 700, color: theme.colors.text, opacity: l3 }}>
          You stay in control.
        </div>
      )}

      {brand > 0.01 && (
        <div style={{ position: 'absolute', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 20, opacity: brand }}>
          <Logo size={56} />
          <div style={{ fontFamily: theme.font, fontSize: 30, fontWeight: 700, color: theme.colors.text }}>
            Apply smarter. Apply faster.
          </div>
        </div>
      )}

      {cta > 0.01 && (
        <div style={{ position: 'absolute', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12, opacity: cta }}>
          <div style={{ fontFamily: theme.font, fontSize: 34, fontWeight: 700, color: theme.colors.text }}>Paste a job link.</div>
          <div style={{ fontFamily: theme.font, fontSize: 34, fontWeight: 700, color: theme.colors.textSecondary }}>
            Let Autogram do the rest.
          </div>
        </div>
      )}

      {finalOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: finalOpacity, transform: `scale(${finalScale})` }}>
          <Logo size={70} />
        </div>
      )}
    </AbsoluteFill>
  );
};
