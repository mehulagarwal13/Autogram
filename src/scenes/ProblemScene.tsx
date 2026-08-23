import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, clickPulseAny } from '../lib/animation';
import { Browser } from '../components/Browser';
import { ChromeWindow } from '../components/ChromeWindow';
import { ApplicationForm, FormField } from '../components/ApplicationForm';
import { Cursor } from '../components/Cursor';

const MONTAGE_START = 40;
const SEG_LEN = 105;

const FIELD_WAYPOINTS = [
  { x: 244, y: 145 },
  { x: 700, y: 145 },
  { x: 244, y: 255 },
  { x: 700, y: 255 },
];

const formSegment = (
  segFrame: number,
  tabs: { label: string; active?: boolean }[],
  url: string,
  fieldDefs: Omit<FormField, 'revealed'>[],
) => {
  const starts = [8, 32, 56, 80];
  const fields: FormField[] = fieldDefs.map((f, i) => ({
    ...f,
    revealed: interpolate(segFrame, [starts[i], starts[i] + 16], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    }),
  }));
  const cursorIndex = Math.min(starts.filter((s) => segFrame >= s).length - 1, 3);
  const wp = FIELD_WAYPOINTS[Math.max(cursorIndex, 0)];
  const click = clickPulseAny(segFrame, starts, 8);

  return (
    <ChromeWindow tabs={tabs} url={url} width={1600} height={880}>
      <ApplicationForm title="Application" fields={fields} />
      <Cursor x={wp.x} y={wp.y} clickProgress={click} />
    </ChromeWindow>
  );
};

const CaptchaSegment: React.FC<{ segFrame: number }> = ({ segFrame }) => {
  const checked = segFrame > 55;
  const click = clickPulseAny(segFrame, [55], 10);
  return (
    <ChromeWindow tabs={[{ label: 'boards.greenhouse.io', active: true }]} url="boards.greenhouse.io/apply" width={1600} height={880}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            padding: '22px 34px',
            borderRadius: 10,
            border: '1.5px solid #D7DAE0',
            background: '#FAFBFC',
          }}
        >
          <div
            style={{
              width: 28,
              height: 28,
              borderRadius: 5,
              border: '2px solid #B7BCC8',
              background: checked ? '#3ECF8E' : '#FFFFFF',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#06231A',
              fontWeight: 800,
            }}
          >
            {checked ? '✓' : ''}
          </div>
          <span style={{ fontSize: 17, color: '#22252E', fontWeight: 600 }}>I&apos;m not a robot</span>
        </div>
      </div>
      <Cursor x={575} y={430} clickProgress={click} />
    </ChromeWindow>
  );
};

const OtpSearchSegment: React.FC<{ segFrame: number }> = ({ segFrame }) => {
  const scrollY = interpolate(segFrame, [0, 105], [0, -60], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const rows = ['LinkedIn Job Alerts', 'Your verification code', 'GitHub weekly digest', 'Indeed — new matches'];
  return (
    <ChromeWindow tabs={[{ label: 'mail.google.com', active: true }]} url="mail.google.com/mail/u/0" width={1600} height={880}>
      <div style={{ transform: `translateY(${scrollY}px)`, padding: '20px 0' }}>
        {rows.map((r, i) => (
          <div
            key={i}
            style={{
              padding: '20px 40px',
              borderBottom: '1px solid #F1F3F4',
              fontSize: 15,
              color: r.includes('verification') ? '#202124' : '#5F6368',
              fontWeight: r.includes('verification') ? 700 : 400,
              background: r.includes('verification') ? '#F2F6FF' : 'transparent',
            }}
          >
            {r}
          </div>
        ))}
      </div>
      <Cursor x={860} y={40} clickProgress={0} />
    </ChromeWindow>
  );
};

export const ProblemScene: React.FC = () => {
  const frame = useCurrentFrame();
  const introOpacity = fadeIn(frame, 0, 20);

  const montageFrame = frame - MONTAGE_START;
  const segIndex = Math.max(0, Math.min(3, Math.floor(montageFrame / SEG_LEN)));
  const segFrame = Math.max(0, montageFrame - segIndex * SEG_LEN);
  const montageOpacity = fadeIn(frame, MONTAGE_START, 15) * fadeOut(frame, 455, 20);

  const h1Opacity = fadeIn(frame, 460, 18) * fadeOut(frame, 522, 16);
  const h2Opacity = fadeIn(frame, 542, 18);

  const nameField: Omit<FormField, 'revealed'> = { label: 'Full Name', value: 'Mehul Agarwal' };
  const emailField: Omit<FormField, 'revealed'> = { label: 'Email Address', value: 'mehul.demo@example.com' };
  const phoneField: Omit<FormField, 'revealed'> = { label: 'Phone Number', value: '+91 98•••••210' };
  const eduField: Omit<FormField, 'revealed'> = { label: 'University', value: 'Jaypee Institute of Info. Tech.' };

  return (
    <AbsoluteFill style={{ background: theme.colors.bg }}>
      <AbsoluteFill
        style={{
          alignItems: 'center',
          justifyContent: 'center',
          opacity: introOpacity,
        }}
      >
        {frame < 480 && (
          <div style={{ opacity: montageOpacity, transform: 'scale(0.92)' }}>
            <Browser width={1600} height={880}>
              {segIndex === 0 && formSegment(segFrame, [
                { label: 'linkedin.com/jobs', active: true },
                { label: 'indeed.com' },
                { label: 'glassdoor.com' },
              ], 'linkedin.com/jobs/view/apply', [nameField, emailField, phoneField, eduField])}
              {segIndex === 1 && formSegment(segFrame, [
                { label: 'linkedin.com/jobs' },
                { label: 'indeed.com', active: true },
                { label: 'glassdoor.com' },
              ], 'indeed.com/apply/form', [
                { label: 'Degree', value: 'Bachelor of Technology' },
                { label: 'Skills', value: 'C++, Python, React' },
                { label: 'Experience', value: '2+ years' },
                { label: 'Cover Letter', value: 'I am excited to apply...' },
              ])}
              {segIndex === 2 && <CaptchaSegment segFrame={segFrame} />}
              {segIndex === 3 && <OtpSearchSegment segFrame={segFrame} />}
            </Browser>
          </div>
        )}
      </AbsoluteFill>

      <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center' }}>
        {h1Opacity > 0.01 && (
          <div
            style={{
              fontFamily: theme.font,
              fontSize: 68,
              fontWeight: 700,
              color: theme.colors.text,
              opacity: h1Opacity,
            }}
          >
            &quot;Applying for one job is easy.&quot;
          </div>
        )}
        {h2Opacity > 0.01 && (
          <div
            style={{
              fontFamily: theme.font,
              fontSize: 68,
              fontWeight: 700,
              color: theme.colors.text,
              opacity: h2Opacity,
            }}
          >
            &quot;Applying for 100 isn&apos;t.&quot;
          </div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
