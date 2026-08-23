import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { clamp01, typeReveal } from '../lib/animation';

/** A labeled text field in the American Express visual style, showing text
 * being "typed" in as `revealed` (0-1) advances. Used by every Amex page
 * component so the typed-field look stays identical across the demo. */
export const TypingAnimation: React.FC<{
  label: string;
  value: string;
  revealed: number; // 0-1
  required?: boolean;
  width?: number | string;
}> = ({ label, value, revealed, required = false, width = '100%' }) => {
  const r = clamp01(revealed);
  const shown = typeReveal(value, r);
  const active = r > 0 && r < 1;

  return (
    <div style={{ width, fontFamily: amexTheme.font }}>
      <div style={{ fontSize: 15, color: amexTheme.colors.body, marginBottom: 8 }}>
        {label} {required && <span style={{ color: amexTheme.colors.required }}>*</span>}
      </div>
      <div
        style={{
          height: 46,
          borderRadius: 6,
          border: `1.5px solid ${active ? amexTheme.colors.borderFocus : amexTheme.colors.border}`,
          background: amexTheme.colors.inputBg,
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          fontSize: 16,
          color: amexTheme.colors.body,
        }}
      >
        {shown}
        {active && (
          <span style={{ display: 'inline-block', width: 2, height: 18, background: amexTheme.colors.borderFocus, marginLeft: 2 }} />
        )}
      </div>
    </div>
  );
};
