import React from 'react';
import { clamp01, typeReveal } from '../lib/animation';

export type FormField = {
  label: string;
  value: string;
  revealed: number; // 0-1: how much of `value` has been "typed" in
  kind?: 'text' | 'question';
  sourceTag?: string; // only rendered once a 'question' field is revealed
};

const FieldBox: React.FC<{ field: FormField }> = ({ field }) => {
  const r = clamp01(field.revealed);
  const shown = typeReveal(field.value, r);
  const filled = r > 0.98;

  if (field.kind === 'question') {
    return (
      <div style={{ gridColumn: '1 / -1', padding: '18px 0', borderTop: '1px solid #E7E9EE' }}>
        <div style={{ fontSize: 16, fontWeight: 600, color: '#22252E', marginBottom: 12 }}>{field.label}</div>
        {r > 0.05 && (
          <div style={{ display: 'inline-flex', flexDirection: 'column', gap: 6 }}>
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                padding: '9px 18px',
                borderRadius: 999,
                background: 'rgba(124,111,250,0.10)',
                border: '1px solid rgba(124,111,250,0.35)',
                color: '#5B4FD6',
                fontSize: 15,
                fontWeight: 700,
                opacity: r,
              }}
            >
              {shown}
            </div>
            {field.sourceTag && r > 0.5 && (
              <div style={{ fontSize: 12, color: '#9099AA', opacity: (r - 0.5) * 2 }}>{field.sourceTag}</div>
            )}
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <div style={{ fontSize: 12.5, color: '#8A90A0', fontWeight: 600, marginBottom: 7, letterSpacing: 0.2 }}>
        {field.label}
      </div>
      <div
        style={{
          height: 42,
          borderRadius: 9,
          border: `1.5px solid ${filled ? 'rgba(62,207,142,0.55)' : '#DDE1E8'}`,
          background: filled ? 'rgba(62,207,142,0.06)' : '#FAFBFC',
          display: 'flex',
          alignItems: 'center',
          padding: '0 14px',
          fontSize: 14.5,
          color: '#22252E',
          fontWeight: 500,
        }}
      >
        {shown}
        {r > 0 && r < 1 && (
          <span
            style={{
              display: 'inline-block',
              width: 2,
              height: 16,
              background: '#7C6FFA',
              marginLeft: 2,
            }}
          />
        )}
      </div>
    </div>
  );
};

export const ApplicationForm: React.FC<{
  title: string;
  fields: FormField[];
  resumeUpload?: { fileName: string; progress: number };
  footerRight?: string;
}> = ({ title, fields, resumeUpload, footerRight }) => {
  return (
    <div style={{ padding: '48px 64px', height: '100%', boxSizing: 'border-box', overflow: 'hidden' }}>
      <div style={{ fontSize: 26, fontWeight: 800, color: '#1A1D24', marginBottom: 30 }}>{title}</div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          gap: '22px 28px',
          maxWidth: 900,
        }}
      >
        {fields.map((f, i) => (
          <FieldBox key={i} field={f} />
        ))}
      </div>

      {resumeUpload && (
        <div
          style={{
            marginTop: 34,
            display: 'inline-flex',
            alignItems: 'center',
            gap: 14,
            padding: '14px 22px',
            borderRadius: 12,
            border: '1.5px solid #DDE1E8',
            background: '#FAFBFC',
          }}
        >
          <span style={{ fontSize: 18 }}>{'📎'}</span>
          <span style={{ fontSize: 14.5, color: '#22252E', fontWeight: 600 }}>{resumeUpload.fileName}</span>
          {resumeUpload.progress >= 1 ? (
            <span style={{ color: '#3ECF8E', fontWeight: 800, fontSize: 15 }}>&#10003;</span>
          ) : (
            <div style={{ width: 60, height: 5, borderRadius: 3, background: '#E7E9EE', overflow: 'hidden' }}>
              <div
                style={{
                  height: '100%',
                  width: `${clamp01(resumeUpload.progress) * 100}%`,
                  background: '#7C6FFA',
                }}
              />
            </div>
          )}
        </div>
      )}

      {footerRight && (
        <div style={{ position: 'absolute', right: 64, bottom: 40, fontSize: 13, color: '#9099AA', fontWeight: 600 }}>
          {footerRight}
        </div>
      )}
    </div>
  );
};
