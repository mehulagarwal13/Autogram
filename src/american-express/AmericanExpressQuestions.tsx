import React from 'react';
import { amexTheme } from '../lib/amexTheme';
import { AmexAppShell } from './AmexAppShell';
import { RadioButton } from '../components/RadioButton';
import { amexJob, amexQuestions } from '../lib/amexData';

export type QuestionBadge = 'detected' | 'sensitive' | 'human' | 'confirmed' | null;

export const AmericanExpressQuestions: React.FC<{
  /** Which question index (into amexQuestions) is currently "in focus". */
  focusedIndex: number;
  /** Per-question answer, keyed by index; undefined = not yet answered. */
  answers: Record<number, 'Yes' | 'No' | undefined>;
  badge: QuestionBadge;
  scrollY: number;
}> = ({ focusedIndex, answers, badge, scrollY }) => {
  return (
    <AmexAppShell jobTitle={amexJob.title} variant="plain">
      <div style={{ transform: `translateY(${scrollY}px)`, display: 'flex', flexDirection: 'column', gap: 34, maxWidth: 900 }}>
        {amexQuestions.map((q, i) => {
          const isFocused = i === focusedIndex;
          const answer = answers[i];
          return (
            <div
              key={i}
              style={{
                paddingLeft: isFocused ? 18 : 0,
                borderLeft: isFocused ? `3px solid ${amexTheme.colors.blue}` : '3px solid transparent',
              }}
            >
              <div style={{ fontFamily: amexTheme.font, fontSize: 15.5, color: amexTheme.colors.body, lineHeight: 1.6, marginBottom: 14 }}>
                {q.question} <span style={{ color: amexTheme.colors.required }}>*</span>
              </div>

              {isFocused && badge && (
                <div style={{ marginBottom: 12 }}>
                  {badge === 'detected' && (
                    <Badge color={amexTheme.colors.muted} text="Application questions detected" />
                  )}
                  {badge === 'sensitive' && <Badge color="#B8860B" text="Sensitive question detected" />}
                  {badge === 'human' && <Badge color="#B8860B" text="Human confirmation required" />}
                  {badge === 'confirmed' && <Badge color="#2E9E52" text="Answer confirmed ✓" />}
                </div>
              )}

              <div style={{ display: 'flex', gap: 40 }}>
                <RadioButton label="Yes" selected={answer === 'Yes'} />
                <RadioButton label="No" selected={answer === 'No'} />
              </div>
            </div>
          );
        })}
      </div>
    </AmexAppShell>
  );
};

const Badge: React.FC<{ color: string; text: string }> = ({ color, text }) => (
  <div
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 8,
      padding: '6px 14px',
      borderRadius: 999,
      background: `${color}1A`,
      border: `1px solid ${color}55`,
      color,
      fontFamily: amexTheme.font,
      fontSize: 12.5,
      fontWeight: 700,
    }}
  >
    {text}
  </div>
);
