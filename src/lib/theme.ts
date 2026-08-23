// Shared design tokens for the Autogram demo video. One place to tune the
// look of every scene/component rather than hardcoding colors per-file.

export const theme = {
  colors: {
    bg: '#0A0C10',
    bgAlt: '#0D1017',
    bgGradientStart: '#0A0C10',
    bgGradientEnd: '#12141C',
    surface: '#12151C',
    surfaceElevated: '#191D27',
    surfaceHover: '#1F2430',
    border: '#262B38',
    borderSoft: '#1B1F2A',
    text: '#F3F5F8',
    textSecondary: '#9BA3B4',
    textMuted: '#6B7280',
    accent: '#7C6FFA',
    accentBright: '#9C8BFF',
    accentSoft: 'rgba(124,111,250,0.16)',
    accentBorder: 'rgba(124,111,250,0.45)',
    accentGradient: 'linear-gradient(135deg, #6D5EF5 0%, #8B7CFF 45%, #5EA8FF 100%)',
    success: '#3ECF8E',
    successSoft: 'rgba(62,207,142,0.16)',
    successBorder: 'rgba(62,207,142,0.45)',
    warning: '#F5A623',
    warningSoft: 'rgba(245,166,35,0.16)',
    warningBorder: 'rgba(245,166,35,0.45)',
    danger: '#F1595E',
    dangerSoft: 'rgba(241,89,94,0.16)',
    dangerBorder: 'rgba(241,89,94,0.45)',
    chromeBar: '#20242E',
    chromeBarDark: '#181B23',
  },
  font: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
  radius: { sm: 8, md: 12, lg: 18, xl: 26, pill: 999 },
  shadow: {
    card: '0 20px 60px rgba(0,0,0,0.45)',
    soft: '0 8px 24px rgba(0,0,0,0.35)',
    glow: '0 0 0 1px rgba(124,111,250,0.35), 0 0 40px rgba(124,111,250,0.25)',
  },
} as const;

export type Theme = typeof theme;
