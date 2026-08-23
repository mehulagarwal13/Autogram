import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { clamp01 } from '../lib/animation';
import { AmexAppShell } from './AmexAppShell';
import { amexJob } from '../lib/amexData';

/** "Confirm Your Identity" — the OTP entry screen. `filledCount` (0-6)
 * drives how many of the six circular boxes show a digit. */
export const AmericanExpressIdentity: React.FC<{
  email: string;
  digits: string[]; // 6 characters
  filledCount: number;
  verifyPressed?: number;
}> = ({ email, digits, filledCount, verifyPressed = 0 }) => {
  const scale = 1 - clamp01(verifyPressed) * 0.05;

  return (
    <AmexAppShell jobTitle={amexJob.title} variant="card">
      <div style={{ textAlign: 'center', fontFamily: amexTheme.headingFont, fontSize: 32, color: amexTheme.colors.blueDark, marginBottom: 22 }}>
        Confirm Your Identity
      </div>
      <div style={{ textAlign: 'center', fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.body, lineHeight: 1.7, marginBottom: 36 }}>
        The verification code was sent to this email address: <strong>{email}</strong>. When you get the code, type
        the code into the field to confirm your identity and complete your job application. Note that it may take
        some time before you receive the code.
      </div>

      <div style={{ display: 'flex', justifyContent: 'center', gap: 14, marginBottom: 32 }}>
        {digits.map((digit, i) => {
          const filled = i < filledCount;
          const isCursor = i === filledCount;
          return (
            <div
              key={i}
              style={{
                width: 56,
                height: 56,
                borderRadius: 12,
                border: `1.5px solid ${isCursor ? amexTheme.colors.borderFocus : amexTheme.colors.border}`,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: 22,
                fontWeight: 700,
                color: amexTheme.colors.body,
                fontFamily: "'SF Mono','Consolas',monospace",
              }}
            >
              {filled ? digit : isCursor ? <span style={{ width: 2, height: 20, background: amexTheme.colors.borderFocus }} /> : ''}
            </div>
          );
        })}
      </div>

      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 20 }}>
        <div
          style={{
            padding: '13px 46px',
            borderRadius: 999,
            border: `1.5px solid ${amexTheme.colors.blue}`,
            color: amexTheme.colors.blue,
            fontFamily: amexTheme.font,
            fontSize: 15,
            fontWeight: 700,
            transform: `scale(${scale})`,
          }}
        >
          VERIFY
        </div>
      </div>

      <div style={{ textAlign: 'center', fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.blue, fontWeight: 600 }}>
        Send New Code
      </div>
    </AmexAppShell>
  );
};
