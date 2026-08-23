import React from 'react';
import { theme } from '../lib/theme';

type Tab = { label: string; active?: boolean };

/** A realistic (but generic) Chrome-style browser chrome — traffic lights,
 * a tab strip, and an address bar — wrapping whatever page content is
 * passed as children. Used for every "outside the Autogram product" screen
 * (job boards, the application form, Gmail) so those visually read as real
 * websites rather than as Autogram's own UI. */
export const ChromeWindow: React.FC<{
  tabs: Tab[];
  url: string;
  children: React.ReactNode;
  width?: number;
  height?: number;
  contentBg?: string;
}> = ({ tabs, url, children, width = 1600, height = 880, contentBg = '#F3F4F6' }) => {
  return (
    <div
      style={{
        width,
        height,
        borderRadius: theme.radius.lg,
        overflow: 'hidden',
        boxShadow: theme.shadow.card,
        border: `1px solid ${theme.colors.border}`,
        display: 'flex',
        flexDirection: 'column',
        fontFamily: theme.font,
      }}
    >
      {/* Title bar: traffic lights + tab strip */}
      <div
        style={{
          background: theme.colors.chromeBarDark,
          display: 'flex',
          alignItems: 'flex-end',
          padding: '10px 12px 0 14px',
          gap: 8,
        }}
      >
        <div style={{ display: 'flex', gap: 8, marginBottom: 12, marginRight: 6 }}>
          {['#F1595E', '#F5A623', '#3ECF8E'].map((c) => (
            <div key={c} style={{ width: 12, height: 12, borderRadius: '50%', background: c }} />
          ))}
        </div>
        {tabs.map((tab, i) => (
          <div
            key={i}
            style={{
              padding: '9px 18px',
              borderRadius: '10px 10px 0 0',
              background: tab.active ? theme.colors.chromeBar : 'transparent',
              color: tab.active ? theme.colors.text : theme.colors.textMuted,
              fontSize: 13,
              fontWeight: 500,
              maxWidth: 180,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {tab.label}
          </div>
        ))}
      </div>
      {/* Address bar */}
      <div
        style={{
          background: theme.colors.chromeBar,
          padding: '10px 16px',
          display: 'flex',
          alignItems: 'center',
          gap: 12,
        }}
      >
        <div style={{ display: 'flex', gap: 10, opacity: 0.5 }}>
          <span style={{ color: theme.colors.textMuted, fontSize: 16 }}>{'←'}</span>
          <span style={{ color: theme.colors.textMuted, fontSize: 16 }}>{'→'}</span>
          <span style={{ color: theme.colors.textMuted, fontSize: 15 }}>{'↻'}</span>
        </div>
        <div
          style={{
            flex: 1,
            background: theme.colors.chromeBarDark,
            borderRadius: theme.radius.pill,
            padding: '8px 16px',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span style={{ fontSize: 13, color: theme.colors.textMuted }}>&#128274;</span>
          <span style={{ fontSize: 14, color: theme.colors.textSecondary, letterSpacing: 0.2 }}>{url}</span>
        </div>
      </div>
      {/* Page content */}
      <div style={{ flex: 1, background: contentBg, position: 'relative', overflow: 'hidden' }}>{children}</div>
    </div>
  );
};
