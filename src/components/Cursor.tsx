import React from 'react';
import { clamp01 } from '../lib/animation';

/** A synthetic mouse pointer, positioned in the parent's coordinate space.
 * `clickProgress` (0-1) drives a click ripple — 0 = idle, rises to 1 right
 * as a click lands, so scenes can time it against a field/button. */
export const Cursor: React.FC<{
  x: number;
  y: number;
  clickProgress?: number;
}> = ({ x, y, clickProgress = 0 }) => {
  const ripple = clamp01(clickProgress);
  const rippleScale = 0.4 + ripple * 1.4;
  const rippleOpacity = ripple > 0 ? (1 - ripple) * 0.6 : 0;
  const pointerScale = 1 - Math.max(0, ripple - 0.6) * 0.3;

  return (
    <div
      style={{
        position: 'absolute',
        left: x,
        top: y,
        zIndex: 60,
        pointerEvents: 'none',
        transform: 'translate(-3px, -2px)',
      }}
    >
      {rippleOpacity > 0 && (
        <div
          style={{
            position: 'absolute',
            left: -12,
            top: -10,
            width: 26,
            height: 26,
            borderRadius: '50%',
            border: '2px solid rgba(124,111,250,0.9)',
            transform: `scale(${rippleScale})`,
            opacity: rippleOpacity,
          }}
        />
      )}
      <svg
        width={22}
        height={26}
        viewBox="0 0 22 26"
        style={{
          transform: `scale(${pointerScale})`,
          filter: 'drop-shadow(0 2px 4px rgba(0,0,0,0.5))',
        }}
      >
        <path
          d="M1.5 1 L1.5 20.5 L6.8 16.2 L9.8 23.5 L13 22.1 L10 15 L18.5 14.7 Z"
          fill="#FFFFFF"
          stroke="#12151C"
          strokeWidth={1.4}
          strokeLinejoin="round"
        />
      </svg>
    </div>
  );
};
