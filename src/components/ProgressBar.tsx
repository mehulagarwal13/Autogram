import React from 'react';
import { theme } from '../lib/theme';
import { clamp01 } from '../lib/animation';

export const ProgressBar: React.FC<{
  progress: number; // 0-1
  label?: string;
  width?: number;
  color?: string;
}> = ({ progress, label, width = 360, color = theme.colors.accent }) => {
  const pct = clamp01(progress);
  return (
    <div style={{ width, fontFamily: theme.font }}>
      {label && (
        <div
          style={{
            fontSize: 15,
            color: theme.colors.textSecondary,
            marginBottom: 10,
            fontWeight: 500,
          }}
        >
          {label}
        </div>
      )}
      <div
        style={{
          width: '100%',
          height: 8,
          borderRadius: theme.radius.pill,
          background: theme.colors.borderSoft,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            height: '100%',
            width: `${pct * 100}%`,
            borderRadius: theme.radius.pill,
            background: theme.colors.accentGradient,
          }}
        />
      </div>
    </div>
  );
};
