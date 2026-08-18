// Autogram — "Paste a job URL" future-vision demo.
// Captures a purpose-built static mockup (D:/PROJECT/autogram/Autogram/autogram-paste-url-demo/mockup/index.html),
// NOT the real Autogram app/backend/extension. Real DOM, real clicks/typing via Playwright,
// styled to match Autogram's actual design system (indigo/fuchsia gradient, Inter, dark glass).
// No server, no login, no real app or real external company was involved.

const BASE = 'file:///D:/PROJECT/autogram/Autogram/autogram-paste-url-demo/mockup/index.html';
const JOB_URL = 'https://meridianfinancial.com/jobs/1847-product-manager';

export default {
  title: 'Autogram — Paste a Job URL (Concept)',
  template: 'sizzle',
  colorScheme: 'dark',
  brand: '#6366f1',

  caption: { theme: 'pill', size: 'md', position: 'bottom' },

  intro: {
    title: 'Autogram',
    subtitle: 'Paste a link. Get an application.',
    script: "Found a job anywhere on the web? Paste that link into Autogram, and let an AI agent turn it into a submitted application.",
  },
  outro: {
    title: 'Find the job anywhere.',
    subtitle: 'Paste the link into Autogram. Let your AI Agent handle the application.',
    script: 'Find the job anywhere. Paste the link into Autogram. Let your AI Agent handle the application.',
  },

  scenes: [
    {
      id: 's1-find-job',
      prepare: async (page) => {
        await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      },
      settleMs: 700,
      target: '#copy-link-btn',
      zoom: 1.15,
      script: "Say Jordan finds this \"Senior Product Manager\" role on Meridian Financial's own careers page. Just \"Copy link\".",
    },
    {
      id: 's2-paste-analyze',
      media: 'clip',
      settleMs: 1300,
      record: async (page, rec) => {
        await rec.click('#url-input');
        await rec.type('#url-input', JOB_URL, { delay: 28 });
        await rec.pause(150);
        await rec.click('#btn-analyze');
        await rec.skipWhile(async () => {
          await page.waitForSelector('#btn-apply-agent.show', { timeout: 10_000 });
        });
        await rec.pause(150);
        await rec.click('#btn-apply-agent');
        await rec.skipWhile(async () => {
          await page.waitForSelector('#view-automation.active', { timeout: 10_000 });
        });
        await rec.pause(600);
      },
      script: 'Paste it into Autogram, hit "Analyze" — link detected, description read, matched to Jordan\'s profile, resume ready. One click on "Apply with AI Agent", and a real browser opens automatically, straight to that exact job page.',
    },
    {
      id: 's3-apply-click',
      settleMs: 600,
      target: '#btn-site-apply',
      zoom: 1.2,
      script: "It lands on the company's own page, and clicks \"Apply for this job\" itself.",
    },
    {
      id: 's4-form-fill',
      media: 'clip',
      settleMs: 600,
      record: async (page, rec) => {
        await rec.click('#f-first');
        await rec.type('#f-first', 'Jordan', { delay: 30 });
        await rec.click('#f-last');
        await rec.type('#f-last', 'Lee', { delay: 30 });
        await rec.click('#f-email');
        await rec.type('#f-email', 'jordan.lee@example.com', { delay: 25 });
        await rec.click('#f-phone');
        await rec.type('#f-phone', '(555) 019-2842', { delay: 25 });
        await rec.click('#f-title');
        await rec.type('#f-title', 'Senior Product Manager', { delay: 25 });
        await rec.select('#f-auth', 'citizen');
        await rec.pause(500);
        await rec.click('#btn-submit');
        await rec.pause(450);
      },
      script: "It fills the form from Jordan's profile — name, contact info, title, work authorization — resume attached automatically. Then, \"Submit Application\".",
    },
    {
      id: 's5-captcha-pause',
      settleMs: 500,
      target: '.captcha-card',
      action: 'none',
      zoom: 1.15,
      script: 'But a verification check stops it cold — Autogram never clicks through: "Human action required. Please complete the verification to continue."',
    },
    {
      id: 's6-resume',
      media: 'clip',
      settleMs: 500,
      record: async (page, rec) => {
        await rec.moveTo('#captcha-check');
        await rec.pause(250);
        await rec.click('#captcha-check');
        await rec.pause(600);
        await rec.skipWhile(async () => {
          await page.waitForSelector('#stage-success.active', { timeout: 10_000 });
        });
        await rec.pause(600);
      },
      script: "One click from Jordan, and it's verified, resumed, and submitted.",
    },
    {
      id: 's7-summary',
      prepare: async (page) => {
        await page.evaluate(() => window.showView('view-summary'));
      },
      settleMs: 800,
      target: '.summary-card',
      action: 'none',
      zoom: 1.12,
      script: 'Back in Autogram: Meridian Financial, the role, a "92%" match, submitted just now.',
    },
  ],
};
