import React from 'react';
import { theme } from '../lib/theme';
import { Logo } from './Logo';

const NAV_ITEMS = ['Dashboard', 'Applications', 'Profile', 'Settings'] as const;

/** The Autogram product's own dark-UI shell — sidebar + content area. Used
 * for every "inside Autogram" screen so it visually reads as one consistent
 * product, distinct from the light "outside web" pages in <ChromeWindow>. */
export const AutogramDashboard: React.FC<{
  active?: (typeof NAV_ITEMS)[number];
  children: React.ReactNode;
  width?: number;
  height?: number;
}> = ({ active = 'Dashboard', children, width = 1760, height = 990 }) => {
  return (
    <div
      style={{
        width,
        height,
        borderRadius: theme.radius.xl,
        overflow: 'hidden',
        display: 'flex',
        background: theme.colors.bg,
        border: `1px solid ${theme.colors.borderSoft}`,
        boxShadow: theme.shadow.card,
        fontFamily: theme.font,
      }}
    >
      <div
        style={{
          width: 240,
          flexShrink: 0,
          background: theme.colors.bgAlt,
          borderRight: `1px solid ${theme.colors.borderSoft}`,
          padding: '30px 20px',
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
        }}
      >
        <div style={{ padding: '0 8px', marginBottom: 34 }}>
          <Logo size={28} />
        </div>
        {NAV_ITEMS.map((item) => {
          const isActive = item === active;
          return (
            <div
              key={item}
              style={{
                padding: '12px 16px',
                borderRadius: theme.radius.md,
                fontSize: 15,
                fontWeight: 600,
                color: isActive ? theme.colors.text : theme.colors.textMuted,
                background: isActive ? theme.colors.surfaceElevated : 'transparent',
              }}
            >
              {item}
            </div>
          );
        })}
      </div>
      <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>{children}</div>
    </div>
  );
};
