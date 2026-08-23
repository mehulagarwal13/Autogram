import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, popIn, stagger } from '../../lib/animation';
import { AutogramDashboard } from '../../components/AutogramDashboard';
import { amexTrackerRows } from '../../lib/amexData';

const STATS_AT = 22;
const ROWS_START = 88;
const ROWS_STAGGER = 24;

const STATUS_COLOR: Record<string, { fg: string; bg: string }> = {
  Applied: { fg: theme.colors.success, bg: theme.colors.successSoft },
  Review: { fg: theme.colors.warning, bg: theme.colors.warningSoft },
};

const STATS = [
  { label: 'Applications completed', value: '2', color: theme.colors.success },
  { label: 'Human actions required', value: '1', color: theme.colors.warning },
  { label: 'Applications awaiting review', value: '1', color: theme.colors.accentBright },
];

export const Tracking: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const sceneOpacity = fadeIn(frame, 0, 16);
  const statsOpacity = fadeIn(frame, STATS_AT, 16);
  const statsScale = 0.92 + popIn(frame, fps, STATS_AT) * 0.08;

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <AutogramDashboard active="Applications">
        <div style={{ height: '100%', padding: '46px 56px', boxSizing: 'border-box' }}>
          <div style={{ fontFamily: theme.font, fontSize: 24, fontWeight: 800, color: theme.colors.text, marginBottom: 26 }}>
            Application History
          </div>

          <div style={{ display: 'flex', gap: 16, marginBottom: 30, opacity: statsOpacity, transform: `scale(${statsScale})` }}>
            {STATS.map((s) => (
              <div key={s.label} style={{ flex: 1, padding: '18px 22px', borderRadius: theme.radius.md, background: theme.colors.surface, border: `1px solid ${theme.colors.borderSoft}` }}>
                <div style={{ fontFamily: theme.font, fontSize: 30, fontWeight: 800, color: s.color }}>{s.value}</div>
                <div style={{ fontFamily: theme.font, fontSize: 13, color: theme.colors.textMuted, fontWeight: 600, marginTop: 4 }}>{s.label}</div>
              </div>
            ))}
          </div>

          <div style={{ borderRadius: theme.radius.md, border: `1px solid ${theme.colors.borderSoft}`, overflow: 'hidden' }}>
            <div style={{ display: 'grid', gridTemplateColumns: '2fr 2fr 1fr 1.4fr', padding: '14px 22px', background: theme.colors.bgAlt, fontFamily: theme.font, fontSize: 13, fontWeight: 700, color: theme.colors.textMuted, letterSpacing: 0.5 }}>
              <span>COMPANY</span>
              <span>ROLE</span>
              <span>STATUS</span>
              <span></span>
            </div>
            {amexTrackerRows.map((row, i) => {
              const opacity = fadeIn(frame, ROWS_START + stagger(i, 0, ROWS_STAGGER), 14);
              const status = STATUS_COLOR[row.status];
              return (
                <div key={row.company} style={{ display: 'grid', gridTemplateColumns: '2fr 2fr 1fr 1.4fr', padding: '16px 22px', borderTop: `1px solid ${theme.colors.borderSoft}`, fontFamily: theme.font, fontSize: 15, color: theme.colors.text, fontWeight: 600, opacity, alignItems: 'center' }}>
                  <span>{row.company}</span>
                  <span style={{ color: theme.colors.textSecondary, fontWeight: 500 }}>{row.role}</span>
                  <span style={{ display: 'inline-flex', width: 'fit-content', padding: '5px 12px', borderRadius: theme.radius.pill, background: status.bg, color: status.fg, fontSize: 13, fontWeight: 700 }}>
                    {row.status}
                  </span>
                  <span style={{ color: theme.colors.textMuted, fontSize: 13 }}>{'note' in row ? row.note : ''}</span>
                </div>
              );
            })}
          </div>
        </div>
      </AutogramDashboard>
    </AbsoluteFill>
  );
};
