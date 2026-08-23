import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { clamp01 } from '../lib/animation';
import { amexJob } from '../lib/amexData';

/** The public job-posting page — recreated from the supplied screenshot:
 * centered serif title, location, a divider, a blue "Apply Now" button,
 * then Job Description / Responsibilities sections. */
export const AmexJobPosting: React.FC<{
  applyGlow?: number;
  applyPressed?: number;
}> = ({ applyGlow = 0, applyPressed = 0 }) => {
  const glow = clamp01(applyGlow);
  const scale = 1 - clamp01(applyPressed) * 0.05;

  return (
    <div style={{ background: amexTheme.colors.pageBg, height: '100%', overflow: 'hidden' }}>
      <div style={{ maxWidth: 920, margin: '0 auto', padding: '56px 40px', textAlign: 'center' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', color: amexTheme.colors.muted, fontSize: 16, marginBottom: 30 }}>
          <span>&#128279;</span>
          <span>&#9734;</span>
        </div>

        <div style={{ fontFamily: amexTheme.headingFont, fontSize: 46, color: amexTheme.colors.heading, marginBottom: 14 }}>
          {amexJob.title}
        </div>
        <div style={{ fontFamily: amexTheme.font, fontSize: 18, color: amexTheme.colors.body, marginBottom: 26 }}>
          {amexJob.location}
        </div>

        <div style={{ width: 60, height: 2, background: amexTheme.colors.border, margin: '0 auto 30px' }} />

        <div
          style={{
            display: 'inline-flex',
            padding: '14px 44px',
            borderRadius: 8,
            background: amexTheme.colors.blue,
            color: '#FFFFFF',
            fontFamily: amexTheme.font,
            fontSize: 16,
            fontWeight: 700,
            transform: `scale(${scale})`,
            boxShadow: glow > 0 ? `0 0 0 ${4 * glow}px rgba(22,87,200,${0.3 * glow})` : 'none',
            marginBottom: 46,
          }}
        >
          Apply Now
        </div>

        <div style={{ textAlign: 'left' }}>
          <div style={{ fontFamily: amexTheme.font, fontSize: 20, fontWeight: 700, color: amexTheme.colors.blue, marginBottom: 14 }}>
            JOB DESCRIPTION
          </div>
          <div style={{ fontFamily: amexTheme.font, fontSize: 16, color: amexTheme.colors.body, lineHeight: 1.7, marginBottom: 34 }}>
            {amexJob.description}
          </div>

          <div style={{ fontFamily: amexTheme.font, fontSize: 20, fontWeight: 700, color: amexTheme.colors.blue, marginBottom: 14 }}>
            RESPONSIBILITIES
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {amexJob.responsibilities.map((line) => (
              <div key={line} style={{ display: 'flex', gap: 10 }}>
                <span style={{ color: amexTheme.colors.muted }}>&#8226;</span>
                <span style={{ fontFamily: amexTheme.font, fontSize: 15.5, color: amexTheme.colors.body, lineHeight: 1.6 }}>
                  {line}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};
