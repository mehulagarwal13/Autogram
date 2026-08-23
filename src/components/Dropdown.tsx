import React from 'react';
import { amexTheme } from '../lib/amexTheme';

type Phase = 'idle' | 'open' | 'selected';

/** An American-Express-style select box: label, bordered box with a
 * chevron, and (in the 'open' phase) a small options panel below with the
 * about-to-be-picked option highlighted — so a selection reads as an
 * actual click on an option, not a magic fill. */
export const Dropdown: React.FC<{
  label: string;
  value: string;
  options: string[];
  phase: Phase;
  required?: boolean;
  width?: number | string;
}> = ({ label, value, options, phase, required = false, width = '100%' }) => {
  const showValue = phase === 'selected';
  const isOpen = phase === 'open';

  return (
    <div style={{ width, fontFamily: amexTheme.font, position: 'relative' }}>
      <div style={{ fontSize: 15, color: amexTheme.colors.body, marginBottom: 8 }}>
        {label} {required && <span style={{ color: amexTheme.colors.required }}>*</span>}
      </div>
      <div
        style={{
          height: 46,
          borderRadius: 6,
          border: `1.5px solid ${isOpen ? amexTheme.colors.borderFocus : amexTheme.colors.border}`,
          background: amexTheme.colors.inputBg,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '0 16px',
          fontSize: 16,
          color: showValue ? amexTheme.colors.body : amexTheme.colors.muted,
        }}
      >
        <span>{showValue ? value : 'Select'}</span>
        <span style={{ color: amexTheme.colors.muted, fontSize: 13, transform: isOpen ? 'rotate(180deg)' : 'none' }}>
          &#9660;
        </span>
      </div>

      {isOpen && (
        <div
          style={{
            position: 'absolute',
            top: 78,
            left: 0,
            right: 0,
            background: '#FFFFFF',
            border: `1px solid ${amexTheme.colors.border}`,
            borderRadius: 6,
            boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
            zIndex: 20,
            overflow: 'hidden',
          }}
        >
          {options.map((opt) => (
            <div
              key={opt}
              style={{
                padding: '11px 16px',
                fontSize: 15,
                color: amexTheme.colors.body,
                background: opt === value ? amexTheme.colors.blueSoft : 'transparent',
              }}
            >
              {opt}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
