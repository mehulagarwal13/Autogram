import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../../lib/theme';
import { fadeIn, fadeOut, clickPulseAny, stagger } from '../../lib/animation';
import { ChromeWindow } from '../../components/ChromeWindow';
import { Cursor } from '../../components/Cursor';
import { AutogramOverlay } from '../../components/AutogramOverlay';
import { AmexEmailAuth } from '../../american-express/AmexEmailAuth';
import { AmericanExpressDocuments } from '../../american-express/AmericanExpressDocuments';
import { AmericanExpressAddress } from '../../american-express/AmericanExpressAddress';
import { AmericanExpressExperience } from '../../american-express/AmericanExpressExperience';
import { amexProfile, amexSkills } from '../../lib/amexData';

const BEAT = {
  email: { start: 20, end: 220 },
  documents: { start: 220, end: 460 },
  address: { start: 460, end: 760 },
  experience: { start: 760, end: 1080 },
  skills: { start: 1080, end: 1200 },
  caption: { start: 1200, end: 1240 },
};

const phaseFor = (local: number, openAt: number, selectAt: number): 'idle' | 'open' | 'selected' =>
  local < openAt ? 'idle' : local < selectAt ? 'open' : 'selected';

const Beat: React.FC<{ start: number; end: number; frame: number; children: (local: number) => React.ReactNode }> = ({
  start,
  end,
  frame,
  children,
}) => {
  if (frame < start - 4 || frame >= end + 4) return null;
  const opacity = fadeIn(frame, start, 14) * fadeOut(frame, end - 16, 14);
  const local = Math.max(0, frame - start);
  return (
    <div style={{ opacity, transform: 'scale(0.94)', position: 'absolute', inset: 0 }}>{children(local)}</div>
  );
};

export const Automation: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 16);

  const overlay = (() => {
    if (frame < BEAT.email.end) return { status: 'working' as const, text: 'Filling application…' };
    if (frame < BEAT.documents.start + 160) return { status: 'working' as const, text: 'Uploading resume…' };
    if (frame < BEAT.documents.end) return { status: 'done' as const, text: 'Resume uploaded' };
    if (frame < BEAT.address.end) return { status: 'working' as const, text: 'Completing address…' };
    if (frame < BEAT.experience.end) return { status: 'working' as const, text: 'Completing experience…' };
    if (frame < BEAT.skills.end) return { status: 'working' as const, text: 'Adding skills…' };
    return { status: 'done' as const, text: 'Application filled' };
  })();

  const captionOpacity = fadeIn(frame, BEAT.caption.start, 16);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      <div style={{ position: 'relative', width: 1680, height: 920 }}>
        <Beat start={BEAT.email.start} end={BEAT.email.end} frame={frame}>
          {(local) => {
            const emailRevealed = interpolate(local, [30, 90], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const checked = local >= 110;
            const nextPressed = clickPulseAny(local, [160], 8);
            return (
              <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/email" width={1680} height={920}>
                <AmexEmailAuth email={amexProfile.email} emailRevealed={emailRevealed} checked={checked} nextPressed={nextPressed} />
              </ChromeWindow>
            );
          }}
        </Beat>

        <Beat start={BEAT.documents.start} end={BEAT.documents.end} frame={frame}>
          {(local) => {
            const resumeProgress = interpolate(local, [50, 160], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            return (
              <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/section/1" width={1680} height={920}>
                <AmericanExpressDocuments resumeProgress={resumeProgress} resumeFileName={amexProfile.resumeFileName} />
              </ChromeWindow>
            );
          }}
        </Beat>

        <Beat start={BEAT.address.start} end={BEAT.address.end} frame={frame}>
          {(local) => {
            const countryPhase = phaseFor(local, 20, 60);
            const addr1Revealed = interpolate(local, [90, 140], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const cityRevealed = interpolate(local, [155, 195], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const pinPhase = phaseFor(local, 210, 245);
            const statePhase = phaseFor(local, 260, 295);
            return (
              <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/section/1" width={1680} height={920}>
                <AmericanExpressAddress
                  countryPhase={countryPhase}
                  addr1Revealed={addr1Revealed}
                  cityRevealed={cityRevealed}
                  pinPhase={pinPhase}
                  statePhase={statePhase}
                />
              </ChromeWindow>
            );
          }}
        </Beat>

        <Beat start={BEAT.experience.start} end={BEAT.experience.end} frame={frame}>
          {(local) => {
            const view = local < 60 ? 'overview' : 'form';
            const addClick = clickPulseAny(local, [50], 8);
            const employerRevealed = interpolate(local, [70, 105], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const jobTitleRevealed = interpolate(local, [112, 142], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const startMonthPhase = phaseFor(local, 150, 172);
            const startYearPhase = phaseFor(local, 178, 198);
            const endMonthPhase = phaseFor(local, 205, 225);
            const endYearPhase = phaseFor(local, 230, 250);
            const responsibilitiesRevealed = interpolate(local, [256, 292], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const addPressed = clickPulseAny(local, [305], 8);
            return (
              <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/section/2" width={1680} height={920}>
                <AmericanExpressExperience
                  view={view}
                  employerRevealed={employerRevealed}
                  jobTitleRevealed={jobTitleRevealed}
                  startMonthPhase={startMonthPhase}
                  startYearPhase={startYearPhase}
                  endMonthPhase={endMonthPhase}
                  endYearPhase={endYearPhase}
                  responsibilitiesRevealed={responsibilitiesRevealed}
                  addPressed={addPressed}
                />
                {local < 60 && <Cursor x={952} y={520} clickProgress={addClick} />}
              </ChromeWindow>
            );
          }}
        </Beat>

        <Beat start={BEAT.skills.start} end={BEAT.skills.end} frame={frame}>
          {(local) => (
            <ChromeWindow tabs={[{ label: 'Software Engineer I', active: true }]} url="careers.americanexpress.com/apply/section/2" width={1680} height={920}>
              <div style={{ padding: '48px 64px' }}>
                <div style={{ fontFamily: theme.font, fontSize: 18, fontWeight: 700, color: '#122B57', marginBottom: 24 }}>Skills</div>
                <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
                  {amexSkills.map((skill, i) => {
                    const delay = stagger(i, 20, 20);
                    const opacity = fadeIn(local, delay, 12);
                    return (
                      <div
                        key={skill}
                        style={{
                          opacity,
                          padding: '10px 22px',
                          borderRadius: 999,
                          background: 'rgba(22,87,200,0.08)',
                          border: '1px solid rgba(22,87,200,0.35)',
                          color: '#1657C8',
                          fontFamily: theme.font,
                          fontSize: 15,
                          fontWeight: 700,
                        }}
                      >
                        {skill}
                      </div>
                    );
                  })}
                </div>
              </div>
            </ChromeWindow>
          )}
        </Beat>
      </div>

      {frame >= BEAT.email.start && frame < BEAT.caption.start && (
        <div style={{ position: 'absolute', top: 56, right: 130 }}>
          <AutogramOverlay status={overlay.status} text={overlay.text} />
        </div>
      )}

      {captionOpacity > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 46, fontWeight: 700, color: theme.colors.text, opacity: captionOpacity, textAlign: 'center', maxWidth: 1300 }}>
          &quot;Autogram turns your profile into a completed application.&quot;
        </div>
      )}
    </AbsoluteFill>
  );
};
