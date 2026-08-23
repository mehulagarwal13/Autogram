// Design tokens for the American Express application-flow recreation used
// in the second demo (AutogramAmericanExpressDemo). Deliberately a plain,
// light corporate look — the point of this composition is that the AmEx
// pages look like the real site, while Autogram's own UI (dark theme, see
// lib/theme.ts) is visually distinct from it.

export const amexTheme = {
  colors: {
    pageBg: '#FFFFFF',
    panelBg: '#FDFDFD',
    breadcrumbBg: '#F4F5F7',
    heading: '#122B57',
    body: '#3C4148',
    muted: '#6B7280',
    border: '#D7DBE1',
    borderFocus: '#2E6FE0',
    blue: '#1657C8',
    blueDark: '#0E3E8F',
    blueSoft: 'rgba(22,87,200,0.08)',
    required: '#C2402E',
    inputBg: '#FFFFFF',
    radioBorder: '#8A93A6',
  },
  font: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif",
  headingFont: "Georgia, 'Times New Roman', serif",
} as const;
