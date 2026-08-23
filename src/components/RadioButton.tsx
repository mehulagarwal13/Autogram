import React from 'react';
import { amexTheme } from '../lib/amexTheme';

export const RadioButton: React.FC<{
  label: string;
  selected: boolean;
}> = ({ label, selected }) => (
  <div style={{ display: 'flex', alignItems: 'center', gap: 10, fontFamily: amexTheme.font }}>
    <div
      style={{
        width: 20,
        height: 20,
        borderRadius: '50%',
        border: `2px solid ${selected ? amexTheme.colors.blue : amexTheme.colors.radioBorder}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
      }}
    >
      {selected && <div style={{ width: 10, height: 10, borderRadius: '50%', background: amexTheme.colors.blue }} />}
    </div>
    <span style={{ fontSize: 15, color: amexTheme.colors.body }}>{label}</span>
  </div>
);
