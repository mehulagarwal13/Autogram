import React from 'react';
import { theme } from '../lib/theme';

/** A minimal laptop-screen bezel around whatever is passed as children
 * (typically a <ChromeWindow>) — used for the "professional at a laptop"
 * framing in the opening scene. Deliberately understated: a thin dark
 * bezel and a camera dot, not an illustrated laptop. */
export const Browser: React.FC<{
  children: React.ReactNode;
  width?: number;
  height?: number;
}> = ({ children, width = 1720, height = 980 }) => {
  const bezel = 22;
  return (
    <div>
      <div
        style={{
          width: width + bezel * 2,
          padding: bezel,
          paddingTop: bezel + 10,
          borderRadius: 22,
          background: 'linear-gradient(180deg, #1B1E27 0%, #0F1116 100%)',
          border: `1px solid ${theme.colors.border}`,
          boxShadow: theme.shadow.card,
        }}
      >
        <div
          style={{
            position: 'absolute',
            top: bezel * 0.42,
            left: '50%',
            transform: 'translateX(-50%)',
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: '#333844',
          }}
        />
        <div style={{ width, height, borderRadius: 8, overflow: 'hidden' }}>{children}</div>
      </div>
      <div
        style={{
          width: width * 0.5,
          height: 14,
          margin: '0 auto',
          background: 'linear-gradient(180deg, #14161C 0%, #0A0B0F 100%)',
          borderRadius: '0 0 10px 10px',
        }}
      />
    </div>
  );
};
