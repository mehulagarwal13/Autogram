import React from 'react';
import { amexTheme } from '../lib/amexTheme';

/** The shell every American-Express application page sits inside: the
 * light breadcrumb bar with a back chevron and the job title (visible on
 * every page after the job posting itself), on a plain white page.
 * `variant="card"` is for the two pages that show their content inside a
 * bordered, centered panel (the email-entry and identity-confirmation
 * screens); `variant="plain"` is for the pages whose fields sit directly
 * on the white background (documents, address, experience, questions). */
export const AmexAppShell: React.FC<{
  jobTitle: string;
  variant: 'card' | 'plain';
  children: React.ReactNode;
  width?: number;
  height?: number;
}> = ({ jobTitle, variant, children, width = 1680, height = 920 }) => {
  return (
    <div
      style={{
        width,
        height,
        background: amexTheme.colors.pageBg,
        overflow: 'hidden',
        display: 'flex',
        flexDirection: 'column',
      }}
    >
      <div
        style={{
          height: 56,
          flexShrink: 0,
          background: amexTheme.colors.breadcrumbBg,
          borderBottom: `1px solid ${amexTheme.colors.border}`,
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '0 32px',
          fontFamily: amexTheme.font,
        }}
      >
        <span style={{ color: amexTheme.colors.heading, fontSize: 18 }}>&#8249;</span>
        <span style={{ color: amexTheme.colors.heading, fontSize: 15, fontWeight: 600 }}>{jobTitle} …</span>
      </div>

      <div style={{ flex: 1, overflow: 'hidden', position: 'relative' }}>
        {variant === 'card' ? (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
            <div
              style={{
                width: 820,
                background: amexTheme.colors.panelBg,
                border: `1px solid ${amexTheme.colors.border}`,
                borderRadius: 10,
                padding: '48px 60px',
              }}
            >
              {children}
            </div>
          </div>
        ) : (
          <div style={{ padding: '40px 64px', height: '100%', boxSizing: 'border-box' }}>{children}</div>
        )}
      </div>
    </div>
  );
};
