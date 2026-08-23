import React from 'react';
import { theme } from '../lib/theme';
import { clamp01 } from '../lib/animation';

type Variant = 'info' | 'success' | 'warning' | 'danger';

const variantColor: Record<Variant, { border: string; bg: string; dot: string }> = {
  info: { border: theme.colors.accentBorder, bg: theme.colors.accentSoft, dot: theme.colors.accentBright },
  success: { border: theme.colors.successBorder, bg: theme.colors.successSoft, dot: theme.colors.success },
  warning: { border: theme.colors.warningBorder, bg: theme.colors.warningSoft, dot: theme.colors.warning },
  danger: { border: theme.colors.dangerBorder, bg: theme.colors.dangerSoft, dot: theme.colors.danger },
};

/** `appear` is a 0-1 progress value the scene computes (slide-in from the
 * right + fade). Positioned by the parent — this just renders the card. */
export const Notification: React.FC<{
  title: string;
  subtitle?: string;
  variant?: Variant;
  appear: number;
}> = ({ title, subtitle, variant = 'info', appear }) => {
  const p = clamp01(appear);
  const x = (1 - p) * 60;
  const colors = variantColor[variant];
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 14,
        minWidth: 340,
        maxWidth: 420,
        padding: '16px 20px',
        borderRadius: theme.radius.lg,
        background: theme.colors.surfaceElevated,
        border: `1px solid ${colors.border}`,
        boxShadow: theme.shadow.card,
        opacity: p,
        transform: `translateX(${x}px)`,
        fontFamily: theme.font,
      }}
    >
      <div
        style={{
          width: 10,
          height: 10,
          borderRadius: '50%',
          background: colors.dot,
          marginTop: 6,
          flexShrink: 0,
          boxShadow: `0 0 12px ${colors.dot}`,
        }}
      />
      <div>
        <div style={{ fontSize: 17, fontWeight: 700, color: theme.colors.text }}>{title}</div>
        {subtitle && (
          <div style={{ fontSize: 14, color: theme.colors.textSecondary, marginTop: 4, lineHeight: 1.4 }}>
            {subtitle}
          </div>
        )}
      </div>
    </div>
  );
};
