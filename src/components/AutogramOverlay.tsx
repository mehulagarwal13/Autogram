import React from 'react';
import { theme } from '../lib/theme';
import { clamp01 } from '../lib/animation';

type Status = 'working' | 'done' | 'warning';

const ICON: Record<Status, string> = { working: '●', done: '✓', warning: '⚠' };
const COLOR: Record<Status, string> = {
  working: theme.colors.accentBright,
  done: theme.colors.success,
  warning: theme.colors.warning,
};

/** The small "Autogram is doing something" pill meant to sit in a corner of
 * a browser window without covering the page underneath it. Deliberately
 * tiny and low-contrast against whatever it's laid over. */
export const AutogramOverlay: React.FC<{
  status: Status;
  text: string;
  appear?: number; // 0-1
}> = ({ status, text, appear = 1 }) => {
  const p = clamp01(appear);
  return (
    <div
      style={{
        display: 'inline-flex',
        flexDirection: 'column',
        gap: 4,
        padding: '10px 16px',
        borderRadius: 10,
        background: 'rgba(10,12,16,0.92)',
        border: `1px solid ${theme.colors.borderSoft}`,
        boxShadow: theme.shadow.soft,
        opacity: p,
        transform: `translateY(${(1 - p) * -8}px)`,
        fontFamily: theme.font,
      }}
    >
      <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: 1.2, color: theme.colors.textMuted }}>AUTOGRAM</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 13, fontWeight: 600, color: COLOR[status] }}>
        <span>{ICON[status]}</span>
        <span style={{ color: theme.colors.text }}>{text}</span>
      </div>
    </div>
  );
};
