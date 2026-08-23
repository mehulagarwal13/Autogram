import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { clamp01 } from '../lib/animation';

/** A drag-and-drop upload zone in the American Express style. `progress`
 * drives three states: empty dashed zone (0), uploading (0-1), uploaded
 * (>=1) — the same box, not a swapped-in different element. */
export const UploadBox: React.FC<{
  kind: string; // "Resume" | "Cover Letter"
  progress: number; // 0-1
  fileName?: string;
}> = ({ kind, progress, fileName }) => {
  const p = clamp01(progress);
  const empty = p <= 0;
  const done = p >= 1;

  return (
    <div
      style={{
        border: `1.5px ${empty ? 'dashed' : 'solid'} ${done ? '#8FCB9E' : amexTheme.colors.border}`,
        borderRadius: 10,
        background: done ? 'rgba(62,180,110,0.06)' : '#FAFBFC',
        padding: '32px 24px',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 12,
        fontFamily: amexTheme.font,
      }}
    >
      {empty ? (
        <>
          <span style={{ fontSize: 30, color: '#B7BCC8' }}>&#9730;</span>
          <span style={{ fontSize: 15, color: amexTheme.colors.body }}>Drop {kind} Here</span>
          <span style={{ fontSize: 13, color: amexTheme.colors.muted }}>or</span>
          <span style={{ fontSize: 15, color: amexTheme.colors.blue, fontWeight: 600 }}>Upload {kind}</span>
        </>
      ) : (
        <>
          <span style={{ fontSize: 15, color: amexTheme.colors.body, fontWeight: 600 }}>{fileName}</span>
          {done ? (
            <span style={{ color: '#2E9E52', fontWeight: 800, fontSize: 16 }}>&#10003; Uploaded</span>
          ) : (
            <div style={{ width: 140, height: 6, borderRadius: 3, background: amexTheme.colors.border, overflow: 'hidden' }}>
              <div style={{ height: '100%', width: `${p * 100}%`, background: amexTheme.colors.blue }} />
            </div>
          )}
        </>
      )}
    </div>
  );
};
