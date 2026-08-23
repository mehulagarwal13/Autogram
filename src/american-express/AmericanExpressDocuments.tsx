import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { AmexAppShell } from './AmexAppShell';
import { UploadBox } from '../components/UploadBox';
import { amexJob } from '../lib/amexData';

export const AmericanExpressDocuments: React.FC<{
  resumeProgress: number; // 0-1
  resumeFileName: string;
}> = ({ resumeProgress, resumeFileName }) => {
  return (
    <AmexAppShell jobTitle={amexJob.title} variant="plain">
      <div style={{ fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.blue, marginBottom: 20 }}>
        You can import your information.
      </div>

      <div
        style={{
          border: `1px solid ${amexTheme.colors.border}`,
          borderRadius: 10,
          padding: '30px 0',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          gap: 16,
          maxWidth: 560,
          marginBottom: 40,
        }}
      >
        <div
          style={{
            padding: '13px 40px',
            borderRadius: 8,
            background: '#003A70',
            color: '#FFFFFF',
            fontFamily: amexTheme.font,
            fontSize: 15,
            fontWeight: 700,
          }}
        >
          Apply with indeed
        </div>
        <div
          style={{
            padding: '13px 60px',
            borderRadius: 999,
            border: `1.5px solid ${amexTheme.colors.blue}`,
            color: amexTheme.colors.blue,
            fontFamily: amexTheme.font,
            fontSize: 15,
            fontWeight: 700,
          }}
        >
          RESUME
        </div>
      </div>

      <div style={{ marginBottom: 20 }}>
        <div style={{ fontFamily: amexTheme.font, fontSize: 17, fontWeight: 700, color: amexTheme.colors.heading, marginBottom: 4 }}>
          Supporting Documents and URLs
        </div>
        <div style={{ fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.muted }}>
          Please add any additional documents or URLs.
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 32, maxWidth: 900, marginBottom: 30 }}>
        <UploadBox kind="Resume" progress={resumeProgress} fileName={resumeFileName} />
        <UploadBox kind="Cover Letter" progress={0} />
      </div>

      <div style={{ maxWidth: 900 }}>
        <div style={{ fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.body, marginBottom: 8 }}>Link 1</div>
        <div style={{ height: 44, borderRadius: 6, border: `1.5px solid ${amexTheme.colors.border}`, background: '#FFFFFF' }} />
      </div>
    </AmexAppShell>
  );
};
