import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, clickPulseAny } from '../lib/animation';
import { ChromeWindow } from '../components/ChromeWindow';
import { ApplicationForm, FormField } from '../components/ApplicationForm';
import { Cursor } from '../components/Cursor';
import { applicationFormFields, demoProfile } from '../lib/data';

const FIELD_START = 24;
const FIELD_GAP = 38;
const FILL_LEN = 20;
const UPLOAD_START = 24 + applicationFormFields.length * FIELD_GAP + 6;
const UPLOAD_LEN = 46;
const HEADLINE_AT = UPLOAD_START + UPLOAD_LEN + 16;

const waypointFor = (i: number) => {
  const col = i % 2;
  const row = Math.floor(i / 2);
  return { x: col === 0 ? 244 : 700, y: 145 + row * 96 };
};

export const AutomationScene: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 18);

  const starts = applicationFormFields.map((_, i) => FIELD_START + i * FIELD_GAP);
  const fields: FormField[] = applicationFormFields.map((f, i) => ({
    ...f,
    revealed: interpolate(frame, [starts[i], starts[i] + FILL_LEN], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    }),
  }));

  const activeIndex = Math.min(
    Math.max(0, starts.filter((s) => frame >= s).length - 1),
    applicationFormFields.length - 1,
  );
  const wp = waypointFor(activeIndex);
  const cursorClick = clickPulseAny(frame, starts, 8);

  const uploadProgress = interpolate(frame, [UPLOAD_START, UPLOAD_START + UPLOAD_LEN], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const formOpacity = fadeOut(frame, HEADLINE_AT - 10, 16);
  const headlineOpacity = fadeIn(frame, HEADLINE_AT, 18);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      {formOpacity > 0.01 && (
        <div style={{ opacity: formOpacity, transform: 'scale(0.94)' }}>
          <ChromeWindow
            tabs={[{ label: 'Software Engineer — AI Platform', active: true }]}
            url="careers.exampletech.com/apply"
            width={1680}
            height={920}
          >
            <ApplicationForm
              title="Application"
              fields={fields}
              resumeUpload={frame >= UPLOAD_START - 8 ? { fileName: demoProfile.resumeFileName, progress: uploadProgress } : undefined}
              footerRight="Filled by Autogram"
            />
            <Cursor x={wp.x} y={wp.y} clickProgress={cursorClick} />
          </ChromeWindow>
        </div>
      )}

      {headlineOpacity > 0.01 && (
        <div
          style={{
            position: 'absolute',
            fontFamily: theme.font,
            fontSize: 62,
            fontWeight: 700,
            color: theme.colors.text,
            opacity: headlineOpacity,
          }}
        >
          &quot;No repetitive typing.&quot;
        </div>
      )}
    </AbsoluteFill>
  );
};
