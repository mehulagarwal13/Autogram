import React from 'react';
import { clamp01 } from '../lib/animation';

/** A realistic (light-themed) job-posting page, as if rendered inside
 * <ChromeWindow>. Not an Autogram screen — this is "the outside web". */
export const JobPosting: React.FC<{
  title: string;
  company: string;
  location: string;
  type?: string;
  description?: string;
  showApplyButton?: boolean;
  applyGlow?: number; // 0-1
  applyPressed?: number; // 0-1
}> = ({ title, company, location, type = 'Full-time', description, showApplyButton = true, applyGlow = 0, applyPressed = 0 }) => {
  const glow = clamp01(applyGlow);
  const scale = 1 - clamp01(applyPressed) * 0.05;
  const initials = company
    .split(' ')
    .map((w) => w[0])
    .join('')
    .slice(0, 2)
    .toUpperCase();

  return (
    <div style={{ padding: '56px 72px', height: '100%', boxSizing: 'border-box' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 18, marginBottom: 28 }}>
        <div
          style={{
            width: 56,
            height: 56,
            borderRadius: 14,
            background: '#E4E7EC',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontWeight: 800,
            fontSize: 20,
            color: '#3A3F4B',
          }}
        >
          {initials}
        </div>
        <div>
          <div style={{ fontSize: 15, color: '#5B6270', fontWeight: 600 }}>{company}</div>
          <div style={{ fontSize: 13, color: '#8A90A0' }}>
            {location} · {type}
          </div>
        </div>
      </div>

      <div style={{ fontSize: 40, fontWeight: 800, color: '#1A1D24', letterSpacing: -0.5, marginBottom: 18, maxWidth: 820 }}>
        {title}
      </div>

      {description && (
        <div style={{ fontSize: 17, color: '#4B5160', lineHeight: 1.7, maxWidth: 760, marginBottom: 40 }}>
          {description}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, maxWidth: 500, marginBottom: 44 }}>
        {[
          'Design and build production services',
          'Collaborate with a small, senior team',
          'Own features end-to-end',
        ].map((line) => (
          <div key={line} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div style={{ width: 6, height: 6, borderRadius: '50%', background: '#B7BCC8' }} />
            <div style={{ fontSize: 15, color: '#5B6270' }}>{line}</div>
          </div>
        ))}
      </div>

      {showApplyButton && (
        <div
          style={{
            display: 'inline-flex',
            padding: '16px 40px',
            borderRadius: 10,
            background: '#1A1D24',
            color: '#FFFFFF',
            fontSize: 17,
            fontWeight: 700,
            transform: `scale(${scale})`,
            boxShadow: glow > 0 ? `0 0 0 ${4 * glow}px rgba(124,111,250,${0.35 * glow})` : 'none',
          }}
        >
          Apply now
        </div>
      )}
    </div>
  );
};
