import React from 'react';
import { theme } from '../lib/theme';

type Variant = 'primary' | 'secondary' | 'success' | 'ghost';

const variantStyles: Record<Variant, React.CSSProperties> = {
  primary: {
    background: theme.colors.accentGradient,
    color: '#0A0C10',
    border: '1px solid transparent',
  },
  secondary: {
    background: theme.colors.surfaceElevated,
    color: theme.colors.text,
    border: `1px solid ${theme.colors.border}`,
  },
  success: {
    background: theme.colors.success,
    color: '#06231A',
    border: '1px solid transparent',
  },
  ghost: {
    background: 'transparent',
    color: theme.colors.textSecondary,
    border: `1px solid ${theme.colors.border}`,
  },
};

/** `pressed` is a 0-1 value the caller animates (e.g. via a cursor-click
 * moment) — 0 is resting, 1 is fully pressed. Kept as a prop rather than
 * internal state so the timing lives in the scene, not the component. */
export const Button: React.FC<{
  label: string;
  variant?: Variant;
  pressed?: number;
  size?: 'md' | 'lg';
  fullWidth?: boolean;
  glow?: boolean;
}> = ({ label, variant = 'primary', pressed = 0, size = 'md', fullWidth = false, glow = false }) => {
  const scale = 1 - pressed * 0.04;
  const isLg = size === 'lg';
  return (
    <div
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: isLg ? '18px 36px' : '12px 24px',
        borderRadius: theme.radius.pill,
        fontFamily: theme.font,
        fontWeight: 700,
        fontSize: isLg ? 22 : 16,
        letterSpacing: 0.2,
        transform: `scale(${scale})`,
        width: fullWidth ? '100%' : undefined,
        boxShadow: glow ? theme.shadow.glow : theme.shadow.soft,
        whiteSpace: 'nowrap',
        ...variantStyles[variant],
      }}
    >
      {label}
    </div>
  );
};
