import React from 'react';
import { clamp01 } from '../lib/animation';
import { verificationEmail as defaultVerificationEmail } from '../lib/data';

const NAV = ['Inbox', 'Starred', 'Sent', 'Drafts'];

export type VerificationEmailData = {
  from: string;
  fromAddress: string;
  subject: string;
  code: string;
  expiry: string;
};

/** A Gmail-like inbox, entirely fictional content. `view` switches between
 * the inbox list and the opened email so the scene can control that beat
 * without mounting/unmounting a different component. `email` defaults to
 * the original Oracle-demo fictional email so the first video's OtpScene
 * keeps working unchanged; other demos pass their own fictional data. */
export const GmailWindow: React.FC<{
  view: 'list' | 'opened';
  codeReveal?: number; // 0-1, for the opened view
  email?: VerificationEmailData;
}> = ({ view, codeReveal = 1, email = defaultVerificationEmail }) => {
  const reveal = clamp01(codeReveal);
  const verificationEmail = email;

  return (
    <div style={{ display: 'flex', height: '100%' }}>
      <div
        style={{
          width: 200,
          flexShrink: 0,
          background: '#F6F8FC',
          padding: '24px 16px',
          borderRight: '1px solid #E4E7EC',
        }}
      >
        <div
          style={{
            padding: '12px 20px',
            borderRadius: 20,
            background: '#FFFFFF',
            boxShadow: '0 1px 4px rgba(0,0,0,0.15)',
            fontSize: 14,
            fontWeight: 700,
            color: '#3C4043',
            marginBottom: 24,
            textAlign: 'center',
          }}
        >
          Compose
        </div>
        {NAV.map((item, i) => (
          <div
            key={item}
            style={{
              padding: '9px 14px',
              borderRadius: '0 16px 16px 0',
              fontSize: 14,
              fontWeight: i === 0 ? 700 : 500,
              color: i === 0 ? '#B93A2E' : '#3C4043',
              background: i === 0 ? '#FCE8E6' : 'transparent',
              marginBottom: 2,
            }}
          >
            {item}
          </div>
        ))}
      </div>

      {view === 'list' ? (
        <div style={{ flex: 1, background: '#FFFFFF' }}>
          {[
            { from: 'LinkedIn', subject: 'New jobs match your profile', time: '9:14 AM', highlight: false },
            {
              from: verificationEmail.from,
              subject: verificationEmail.subject,
              time: '9:41 AM',
              highlight: true,
            },
            { from: 'GitHub', subject: 'Weekly digest for your repositories', time: 'Yesterday', highlight: false },
          ].map((row, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 16,
                padding: '16px 28px',
                borderBottom: '1px solid #F1F3F4',
                background: row.highlight ? '#F2F6FF' : '#FFFFFF',
              }}
            >
              <div
                style={{
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: row.highlight ? '#3E6BFF' : 'transparent',
                  flexShrink: 0,
                }}
              />
              <div style={{ width: 160, fontSize: 14, fontWeight: row.highlight ? 700 : 500, color: '#202124' }}>
                {row.from}
              </div>
              <div style={{ flex: 1, fontSize: 14, color: row.highlight ? '#202124' : '#5F6368', fontWeight: row.highlight ? 600 : 400 }}>
                {row.subject}
              </div>
              <div style={{ fontSize: 12.5, color: '#80868B' }}>{row.time}</div>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ flex: 1, background: '#FFFFFF', padding: '36px 48px' }}>
          <div style={{ fontSize: 22, fontWeight: 700, color: '#202124', marginBottom: 18 }}>
            {verificationEmail.subject}
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 30 }}>
            <div
              style={{
                width: 40,
                height: 40,
                borderRadius: '50%',
                background: '#3E6BFF',
                color: '#FFFFFF',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontWeight: 700,
                fontSize: 16,
              }}
            >
              E
            </div>
            <div>
              <div style={{ fontSize: 14, fontWeight: 600, color: '#202124' }}>{verificationEmail.from}</div>
              <div style={{ fontSize: 12.5, color: '#80868B' }}>{verificationEmail.fromAddress}</div>
            </div>
          </div>

          <div style={{ fontSize: 15, color: '#3C4043', lineHeight: 1.7, marginBottom: 28 }}>
            Use the verification code below to confirm your application. This is a demo email — no real account is
            involved.
          </div>

          <div style={{ fontSize: 13, color: '#80868B', fontWeight: 600, marginBottom: 8 }}>
            Your verification code
          </div>
          <div
            style={{
              display: 'inline-block',
              padding: '18px 32px',
              borderRadius: 12,
              background: '#F6F8FC',
              border: '1.5px dashed #C7D2E8',
              fontSize: 34,
              fontWeight: 800,
              letterSpacing: 6,
              color: '#202124',
              fontFamily: "'SF Mono', 'Consolas', monospace",
              opacity: 0.15 + reveal * 0.85,
              filter: `blur(${(1 - reveal) * 6}px)`,
            }}
          >
            {verificationEmail.code}
          </div>
          <div style={{ fontSize: 13, color: '#80868B', marginTop: 14 }}>{verificationEmail.expiry}</div>
        </div>
      )}
    </div>
  );
};
