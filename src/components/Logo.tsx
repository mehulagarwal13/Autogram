import React from 'react';
import { theme } from '../lib/theme';

export const Logo: React.FC<{
  size?: number;
  showWordmark?: boolean;
  color?: string;
}> = ({ size = 32, showWordmark = true, color = theme.colors.text }) => {
  const markSize = size;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: markSize * 0.35 }}>
      <div
        style={{
          width: markSize,
          height: markSize,
          borderRadius: markSize * 0.28,
          background: theme.colors.accentGradient,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          boxShadow: '0 4px 16px rgba(124,111,250,0.35)',
          flexShrink: 0,
        }}
      >
        <span
          style={{
            fontFamily: theme.font,
            fontWeight: 800,
            fontSize: markSize * 0.55,
            color: '#0A0C10',
            lineHeight: 1,
          }}
        >
          A
        </span>
      </div>
      {showWordmark && (
        <span
          style={{
            fontFamily: theme.font,
            fontWeight: 700,
            fontSize: markSize * 0.62,
            letterSpacing: -0.3,
            color,
          }}
        >
          Autogram
        </span>
      )}
    </div>
  );
};
