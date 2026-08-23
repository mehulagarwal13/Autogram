import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { AmexAppShell } from './AmexAppShell';
import { TypingAnimation } from '../components/TypingAnimation';
import { Dropdown } from '../components/Dropdown';
import { amexJob, amexExperience } from '../lib/amexData';

type Phase = 'idle' | 'open' | 'selected';

const SectionButton: React.FC<{ label: string }> = ({ label }) => (
  <div
    style={{
      display: 'inline-flex',
      padding: '14px 30px',
      borderRadius: 8,
      background: amexTheme.colors.blue,
      color: '#FFFFFF',
      fontFamily: amexTheme.font,
      fontSize: 15,
      fontWeight: 700,
    }}
  >
    {label}
  </div>
);

const SectionOverview: React.FC = () => (
  <div style={{ display: 'flex', flexDirection: 'column', gap: 44 }}>
    {[
      { title: 'Education', required: true, help: 'Please provide details about your education.', button: 'Add Education' },
      { title: 'Experience', required: false, help: 'Please provide details about your work experience.', button: 'Add Experience' },
      { title: 'Skills', required: false, help: 'Add your skills.', button: 'Add Skill' },
      { title: 'Licenses and Certificates', required: false, help: 'Please provide details about your licenses and certificates.', button: 'Add License' },
    ].map((section) => (
      <div key={section.title} style={{ textAlign: 'center' }}>
        <div style={{ fontFamily: amexTheme.font, fontSize: 17, fontWeight: 700, color: amexTheme.colors.heading }}>
          {section.title} {section.required && <span style={{ color: amexTheme.colors.required }}>*</span>}
        </div>
        <div style={{ fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.muted, marginBottom: 18 }}>
          {section.help}
        </div>
        <div style={{ display: 'flex', justifyContent: 'center' }}>
          <SectionButton label={section.button} />
        </div>
      </div>
    ))}
  </div>
);

export const AmericanExpressExperience: React.FC<{
  view: 'overview' | 'form';
  employerRevealed?: number;
  jobTitleRevealed?: number;
  startMonthPhase?: Phase;
  startYearPhase?: Phase;
  endMonthPhase?: Phase;
  endYearPhase?: Phase;
  responsibilitiesRevealed?: number;
  addPressed?: number;
}> = ({
  view,
  employerRevealed = 0,
  jobTitleRevealed = 0,
  startMonthPhase = 'idle',
  startYearPhase = 'idle',
  endMonthPhase = 'idle',
  endYearPhase = 'idle',
  responsibilitiesRevealed = 0,
  addPressed = 0,
}) => {
  return (
    <AmexAppShell jobTitle={amexJob.title} variant="plain">
      {view === 'overview' ? (
        <SectionOverview />
      ) : (
        <div style={{ maxWidth: 640 }}>
          <div style={{ fontFamily: amexTheme.font, fontSize: 18, fontWeight: 700, color: amexTheme.colors.heading, marginBottom: 4 }}>
            Experience
          </div>
          <div style={{ fontFamily: amexTheme.font, fontSize: 14, color: amexTheme.colors.muted, marginBottom: 26 }}>
            Please provide details about your work experience.
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 22 }}>
            <TypingAnimation label="Employer Name" required value={amexExperience.employerName} revealed={employerRevealed} />
            <TypingAnimation label="Job Title" value={amexExperience.jobTitle} revealed={jobTitleRevealed} />

            <div>
              <div style={{ fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.body, marginBottom: 8 }}>Start Date</div>
              <div style={{ display: 'flex', gap: 14 }}>
                <Dropdown label="" value={amexExperience.startMonth} options={['Jan', 'Feb', amexExperience.startMonth, 'Jul']} phase={startMonthPhase} width={200} />
                <Dropdown label="" value={amexExperience.startYear} options={['2022', amexExperience.startYear, '2024']} phase={startYearPhase} width={140} />
              </div>
            </div>

            <div>
              <div style={{ fontFamily: amexTheme.font, fontSize: 15, color: amexTheme.colors.body, marginBottom: 8 }}>End Date</div>
              <div style={{ display: 'flex', gap: 14 }}>
                <Dropdown label="" value={amexExperience.endMonth} options={['Jul', amexExperience.endMonth, 'Sep']} phase={endMonthPhase} width={200} />
                <Dropdown label="" value={amexExperience.endYear} options={['2022', amexExperience.endYear, '2024']} phase={endYearPhase} width={140} />
              </div>
            </div>

            <TypingAnimation label="Responsibilities" value={amexExperience.responsibilities} revealed={responsibilitiesRevealed} />

            <div style={{ display: 'flex', gap: 14, marginTop: 8 }}>
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
                  transform: `scale(${1 - Math.min(1, Math.max(0, addPressed)) * 0.04})`,
                }}
              >
                Add Experience
              </div>
            </div>
          </div>
        </div>
      )}
    </AmexAppShell>
  );
};
