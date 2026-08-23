import React from 'react';
import { AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, clickPulseAny, popIn } from '../../lib/animation';
import { Browser } from '../../components/Browser';
import { ChromeWindow } from '../../components/ChromeWindow';
import { ApplicationForm, FormField } from '../../components/ApplicationForm';
import { Cursor } from '../../components/Cursor';
import { Logo } from '../../components/Logo';

const MONTAGE_START = 20;
const SEG_LEN = 100;

const WAYPOINTS = [
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
  const starts = [8, 30, 52, 74];
  const fields: FormField[] = fieldDefs.map((f, i) => ({
    ...f,
    revealed: interpolate(segFrame, [starts[i], starts[i] + 16], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' }),
  }));
  const idx = Math.min(starts.filter((s) => segFrame >= s).length - 1, 3);
  const wp = WAYPOINTS[Math.max(idx, 0)];
  const click = clickPulseAny(segFrame, starts, 8);
  return (
    <ChromeWindow tabs={tabs} url={url} width={1600} height={880}>
      <ApplicationForm title="Application" fields={fields} />
      <Cursor x={wp.x} y={wp.y} clickProgress={click} />
    </ChromeWindow>
  );
};

const CaptchaGmailSegment: React.FC<{ segFrame: number }> = ({ segFrame }) => {
  const checked = segFrame > 60;
  const click = clickPulseAny(segFrame, [60], 10);
  return (
    <ChromeWindow tabs={[{ label: 'boards.example.com', active: true }]} url="boards.example.com/apply" width={1600} height={880}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16, padding: '22px 34px', borderRadius: 10, border: '1.5px solid #D7DAE0', background: '#FAFBFC' }}>
          <div style={{ width: 28, height: 28, borderRadius: 5, border: '2px solid #B7BCC8', background: checked ? '#3ECF8E' : '#FFFFFF', display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#06231A', fontWeight: 800 }}>
            {checked ? '✓' : ''}
          </div>
          <span style={{ fontSize: 17, color: '#22252E', fontWeight: 600 }}>I&apos;m not a robot</span>
        </div>
      </div>
      <Cursor x={575} y={430} clickProgress={click} />
    </ChromeWindow>
  );
};

export const Problem: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const introOpacity = fadeIn(frame, 0, 16);

  const montageFrame = frame - MONTAGE_START;
  const segIndex = Math.max(0, Math.min(2, Math.floor(montageFrame / SEG_LEN)));
  const segFrame = Math.max(0, montageFrame - segIndex * SEG_LEN);
  const montageOpacity = fadeIn(frame, MONTAGE_START, 15) * fadeOut(frame, 315, 18);

  const h1Opacity = fadeIn(frame, 330, 16) * fadeOut(frame, 380, 14);
  const h2Opacity = fadeIn(frame, 395, 16) * fadeOut(frame, 428, 12);

  const logoOpacity = fadeIn(frame, 438, 14);
  const logoScale = popIn(frame, fps, 438);
  const taglineOpacity = fadeIn(frame, 458, 14);

  const nameField: Omit<FormField, 'revealed'> = { label: 'Full Name', value: 'Mehul Agarwal' };
  const emailField: Omit<FormField, 'revealed'> = { label: 'Email Address', value: 'user@example.com' };
  const phoneField: Omit<FormField, 'revealed'> = { label: 'Phone Number', value: '+91 98•••••210' };
  const locField: Omit<FormField, 'revealed'> = { label: 'Location', value: 'Bengaluru, India' };

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center' }}>
      <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center', opacity: introOpacity }}>
        {frame < 335 && (
          <div style={{ opacity: montageOpacity, transform: 'scale(0.92)' }}>
            <Browser width={1600} height={880}>
              {segIndex === 0 &&
                formSegment(
                  segFrame,
                  [{ label: 'linkedin.com/jobs', active: true }, { label: 'indeed.com' }, { label: 'glassdoor.com' }],
                  'linkedin.com/jobs/view/apply',
                  [nameField, emailField, phoneField, locField],
                )}
              {segIndex === 1 &&
                formSegment(
                  segFrame,
                  [{ label: 'linkedin.com/jobs' }, { label: 'indeed.com', active: true }, { label: 'glassdoor.com' }],
                  'indeed.com/apply/form',
                  [
                    { label: 'Degree', value: 'Bachelor of Technology' },
                    { label: 'Skills', value: 'C++, Python, React' },
                    { label: 'Experience', value: '2+ years' },
                    { label: 'Cover Letter', value: 'I am excited to apply...' },
                  ],
                )}
              {segIndex === 2 && <CaptchaGmailSegment segFrame={segFrame} />}
            </Browser>
          </div>
        )}
      </AbsoluteFill>

      <AbsoluteFill style={{ alignItems: 'center', justifyContent: 'center' }}>
        {h1Opacity > 0.01 && (
          <div style={{ fontFamily: theme.font, fontSize: 60, fontWeight: 700, color: theme.colors.text, opacity: h1Opacity, textAlign: 'center', maxWidth: 1300 }}>
            &quot;Every application asks for the same information.&quot;
          </div>
        )}
        {h2Opacity > 0.01 && (
          <div style={{ fontFamily: theme.font, fontSize: 60, fontWeight: 700, color: theme.colors.text, opacity: h2Opacity, textAlign: 'center' }}>
            &quot;Again. And again. And again.&quot;
          </div>
        )}
        {logoOpacity > 0.01 && (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 22, opacity: logoOpacity, transform: `scale(${logoScale})` }}>
            <Logo size={64} />
            <div style={{ fontFamily: theme.font, fontSize: 26, fontWeight: 500, color: theme.colors.textSecondary, opacity: taglineOpacity }}>
              Your AI job application agent.
            </div>
          </div>
        )}
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
