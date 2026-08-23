import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, clickPulseAny } from '../../lib/animation';
import { ChromeWindow } from '../../components/ChromeWindow';
import { GmailWindow } from '../../components/GmailWindow';
import { Cursor } from '../../components/Cursor';
import { AmericanExpressIdentity } from '../../american-express/AmericanExpressIdentity';
import { amexProfile, amexVerificationEmail } from '../../lib/amexData';

const PAUSE_BEAT_END = 62;
const GMAIL_LIST_END = 150;
const GMAIL_OPEN_END = 235;
const BACK_TO_APP_END = 255;
const OTP_ENTER_END = 320;
const HEADLINE_AT = 345;

const otpDigits = amexVerificationEmail.code.replace(/\s/g, '').split('');

export const Otp: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const pausedOpacity = fadeIn(frame, 16, 14) * fadeOut(frame, PAUSE_BEAT_END - 14, 14);

  const gmailListOpacity = fadeIn(frame, PAUSE_BEAT_END, 16) * fadeOut(frame, GMAIL_LIST_END - 14, 14);
  const openClick = clickPulseAny(frame, [GMAIL_LIST_END - 20], 8);

  const gmailOpenOpacity = fadeIn(frame, GMAIL_LIST_END, 16) * fadeOut(frame, GMAIL_OPEN_END - 14, 14);
  const codeReveal = interpolate(frame, [GMAIL_LIST_END + 20, GMAIL_LIST_END + 55], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const appBackOpacity = fadeIn(frame, GMAIL_OPEN_END, 16);
  const otpFilledCount = Math.round(
    interpolate(frame, [BACK_TO_APP_END, OTP_ENTER_END], [0, otpDigits.length], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    }),
  );
  const verifyPressed = clickPulseAny(frame, [OTP_ENTER_END + 4], 8);
  const verifiedOpacity = fadeIn(frame, OTP_ENTER_END + 10, 14);

  const headlineOpacity = fadeIn(frame, HEADLINE_AT, 16);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      {pausedOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: pausedOpacity, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16 }}>
          <div
            style={{
              fontFamily: theme.font,
              fontSize: 15,
              fontWeight: 800,
              letterSpacing: 1.5,
              color: theme.colors.warning,
              background: theme.colors.warningSoft,
              border: `1px solid ${theme.colors.warningBorder}`,
              padding: '8px 20px',
              borderRadius: theme.radius.pill,
            }}
          >
            OTP VERIFICATION REQUIRED
          </div>
          <div style={{ fontFamily: theme.font, fontSize: 22, fontWeight: 600, color: theme.colors.textSecondary }}>
            Autogram has paused — check your email for a code.
          </div>
        </div>
      )}

      {gmailListOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: gmailListOpacity, transform: 'scale(0.94)' }}>
          <ChromeWindow tabs={[{ label: 'Gmail', active: true }]} url="mail.google.com/mail/u/0" width={1680} height={920}>
            <GmailWindow view="list" email={amexVerificationEmail} />
            <Cursor x={640} y={166} clickProgress={openClick} />
          </ChromeWindow>
        </div>
      )}

      {gmailOpenOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: gmailOpenOpacity, transform: 'scale(0.94)' }}>
          <ChromeWindow tabs={[{ label: 'Gmail', active: true }]} url="mail.google.com/mail/u/0" width={1680} height={920}>
            <GmailWindow view="opened" codeReveal={codeReveal} email={amexVerificationEmail} />
          </ChromeWindow>
        </div>
      )}

      {appBackOpacity > 0.01 && (
        <div style={{ position: 'absolute', opacity: appBackOpacity, transform: 'scale(0.94)' }}>
          <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/identity" width={1680} height={920}>
            <AmericanExpressIdentity email={amexProfile.email} digits={otpDigits} filledCount={otpFilledCount} verifyPressed={verifyPressed} />
            {verifiedOpacity > 0.01 && (
              <div
                style={{
                  position: 'absolute',
                  bottom: 60,
                  left: '50%',
                  transform: 'translateX(-50%)',
                  opacity: verifiedOpacity,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  fontFamily: theme.font,
                  fontSize: 18,
                  fontWeight: 700,
                  color: '#1FA971',
                }}
              >
                <span>&#10003;</span>
                <span>Identity verified</span>
              </div>
            )}
          </ChromeWindow>
        </div>
      )}

      {headlineOpacity > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 54, fontWeight: 700, color: theme.colors.text, opacity: headlineOpacity, textAlign: 'center' }}>
          &quot;Identity verified — Autogram resumes.&quot;
        </div>
      )}
    </AbsoluteFill>
  );
};
