import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, popIn } from '../lib/animation';
import { Logo } from '../components/Logo';

const LOGO_AT = 16;
const STATEMENT1_AT = 46;
const STATEMENT2_AT = 116;
const CTA_AT = 182;
const FINAL_LOGO_AT = 248;

export const FinalScene: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const smallLogoOpacity = fadeIn(frame, LOGO_AT, 16) * fadeOut(frame, FINAL_LOGO_AT - 20, 16);
  const s1Opacity = fadeIn(frame, STATEMENT1_AT, 16) * fadeOut(frame, STATEMENT2_AT - 14, 14);
  const s2Opacity = fadeIn(frame, STATEMENT2_AT, 16) * fadeOut(frame, CTA_AT - 14, 14);
  const ctaOpacity = fadeIn(frame, CTA_AT, 16) * fadeOut(frame, FINAL_LOGO_AT - 10, 14);

  const finalOpacity = fadeIn(frame, FINAL_LOGO_AT, 20);
  const finalScale = 0.85 + popIn(frame, fps, FINAL_LOGO_AT) * 0.15;

  return (
    <AbsoluteFill
      style={{
        background: `radial-gradient(circle at 50% 40%, ${theme.colors.bgGradientEnd} 0%, ${theme.colors.bg} 70%)`,
        alignItems: 'center',
        justifyContent: 'center',
        opacity: sceneOpacity,
      }}
    >
      {smallLogoOpacity > 0.01 && (
        <div style={{ position: 'absolute', top: 90, opacity: smallLogoOpacity }}>
          <Logo size={40} />
        </div>
      )}

      {s1Opacity > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 66, fontWeight: 800, color: theme.colors.text, opacity: s1Opacity, textAlign: 'center' }}>
          Apply smarter. Apply faster.
        </div>
      )}
      {s2Opacity > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 66, fontWeight: 800, color: theme.colors.text, opacity: s2Opacity, textAlign: 'center' }}>
          Your job search, automated.
        </div>
      )}

      {ctaOpacity > 0.01 && (
        <div style={{ position: 'absolute', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14, opacity: ctaOpacity }}>
          <div style={{ fontFamily: theme.font, fontSize: 40, fontWeight: 700, color: theme.colors.text }}>Paste a job link.</div>
          <div style={{ fontFamily: theme.font, fontSize: 40, fontWeight: 700, color: theme.colors.textSecondary }}>
            Let Autogram do the rest.
          </div>
        </div>
      )}

      {finalOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: finalOpacity, transform: `scale(${finalScale})` }}>
          <Logo size={72} />
        </div>
      )}
    </AbsoluteFill>
  );
};
