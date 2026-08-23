import React from 'react';
import { useCurrentFrame } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, slideUpPx } from '../lib/animation';

/** Big centered (or left-aligned) statement text, one line at a time, each
 * fading + sliding up into place — the "Display: ..." moments used across
 * almost every scene. `appearAt` is a local frame number (relative to the
 * scene's own Sequence). */
export const Headline: React.FC<{
  lines: string[];
  appearAt: number;
  size?: number;
  color?: string;
  weight?: number;
  align?: 'center' | 'left';
  gap?: number;
  maxWidth?: number;
  lineStagger?: number;
}> = ({
  lines,
  appearAt,
  size = 60,
  color = theme.colors.text,
  weight = 700,
  align = 'center',
  gap = 10,
  maxWidth = 1200,
  lineStagger = 10,
}) => {
  const frame = useCurrentFrame();
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: align === 'center' ? 'center' : 'flex-start',
        gap,
        maxWidth,
        textAlign: align,
      }}
    >
      {lines.map((line, i) => {
        const delay = appearAt + i * lineStagger;
        const opacity = fadeIn(frame, delay, 18);
        const y = slideUpPx(frame, delay, 18, 26);
        return (
          <div
            key={i}
            style={{
              fontSize: size,
              fontWeight: weight,
              color,
              lineHeight: 1.15,
              letterSpacing: -0.5,
              opacity,
              transform: `translateY(${y}px)`,
              fontFamily: theme.font,
            }}
          >
            {line}
          </div>
        );
      })}
    </div>
  );
};
