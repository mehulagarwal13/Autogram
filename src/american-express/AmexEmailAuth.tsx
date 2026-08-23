import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { typeReveal, clamp01 } from '../lib/animation';
import { AmexAppShell } from './AmexAppShell';
import { amexJob } from '../lib/amexData';

/** "You don't need to have an account" — the passwordless email-entry step. */
export const AmexEmailAuth: React.FC<{
  email: string;
  emailRevealed: number; // 0-1
  checked: boolean;
  nextPressed?: number;
}> = ({ email, emailRevealed, checked, nextPressed = 0 }) => {
  const r = clamp01(emailRevealed);
  const shown = typeReveal(email, r);
  const active = r > 0 && r < 1;
  const filled = r >= 0.98;
  const scale = 1 - clamp01(nextPressed) * 0.04;

  return (
    <AmexAppShell jobTitle={amexJob.title} variant="card">
      <div style={{ textAlign: 'center', fontFamily: amexTheme.headingFont, fontSize: 30, color: amexTheme.colors.blueDark, marginBottom: 22 }}>
        You don&apos;t need to have an account
      </div>
      <div style={{ textAlign: 'center', fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.body, lineHeight: 1.6, marginBottom: 30 }}>
        Get started right away by simply using your email. Your profile will be created and kept up to date
        automatically as you enter details for each of your job applications.
      </div>

      <div style={{ fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.body, marginBottom: 8 }}>
        Email Address <span style={{ color: amexTheme.colors.required }}>*</span>
      </div>
      <div
        style={{
          height: 46,
          borderRadius: 6,
          border: `1.5px solid ${active ? amexTheme.colors.borderFocus : amexTheme.colors.border}`,
          background: filled ? amexTheme.colors.blueSoft : amexTheme.colors.inputBg,
          display: 'flex',
          alignItems: 'center',
          padding: '0 16px',
          fontSize: 16,
          color: amexTheme.colors.body,
          marginBottom: 10,
        }}
      >
        {shown}
        {active && <span style={{ display: 'inline-block', width: 2, height: 18, background: amexTheme.colors.borderFocus, marginLeft: 2 }} />}
      </div>
      <div style={{ fontFamily: amexTheme.font, fontSize: 13, color: amexTheme.colors.muted, marginBottom: 26 }}>
        This is how we&apos;ll communicate with you.
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 34 }}>
        <div
          style={{
            width: 20,
            height: 20,
            borderRadius: 4,
            border: `2px solid ${checked ? amexTheme.colors.blue : amexTheme.colors.radioBorder}`,
            background: checked ? amexTheme.colors.blue : '#FFFFFF',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#FFFFFF',
            fontWeight: 800,
            fontSize: 12,
          }}
        >
          {checked ? '✓' : ''}
        </div>
        <span style={{ fontFamily: amexTheme.font, fontSize: 14.5, color: amexTheme.colors.body }}>
          I agree with the <span style={{ color: amexTheme.colors.blue }}>terms and conditions</span>{' '}
          <span style={{ color: amexTheme.colors.required }}>*</span>
        </span>
      </div>

      <div style={{ display: 'flex', gap: 14 }}>
        <div
          style={{
            padding: '13px 34px',
            borderRadius: 8,
            border: `1.5px solid ${amexTheme.colors.blue}`,
            color: amexTheme.colors.blue,
            fontFamily: amexTheme.font,
            fontSize: 15,
            fontWeight: 700,
          }}
        >
          Cancel
        </div>
        <div
          style={{
            padding: '13px 34px',
            borderRadius: 8,
            background: amexTheme.colors.blue,
            color: '#FFFFFF',
            fontFamily: amexTheme.font,
            fontSize: 15,
            fontWeight: 700,
            transform: `scale(${scale})`,
          }}
        >
          Next
        </div>
      </div>
    </AmexAppShell>
  );
};
