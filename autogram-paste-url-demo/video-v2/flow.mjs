// Autogram — "Paste a job URL" future-vision demo (v2: tighter cut, premium visuals).
// Captures a purpose-built static mockup (D:/PROJECT/autogram/Autogram/autogram-paste-url-demo/mockup/index.html),
// NOT the real Autogram app/backend/extension. Real DOM, real clicks/typing via Playwright,
// styled to match Autogram's actual design system (indigo/fuchsia gradient, Inter, dark glass).
// Company/site is entirely fictional ("Northbridge Financial") - no real brand is depicted.
// No server, no login, no real app was involved.

const BASE = 'file:///D:/PROJECT/autogram/Autogram/autogram-paste-url-demo/mockup/index.html';
const JOB_URL = 'https://northbridgefinancial.com/jobs/1847-product-manager';

export default {
  title: 'Autogram — Paste a Job URL (Concept v2)',
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
      id: 'n1-find-job',
      prepare: async (page) => {
        await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      },
      settleMs: 700,
      target: '#copy-link-btn',
      zoom: 1.15,
      script: "Say Jordan finds this \"Senior Product Manager\" role on Northbridge Financial's own careers page. Just \"Copy link\".",
    },
    {
      id: 'n2-apply-and-land',
      media: 'clip',
      settleMs: 900,
      record: async (page, rec) => {
        await rec.click('#url-input');
        await rec.type('#url-input', JOB_URL, { delay: 24 });
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
        await rec.pause(500);
        await rec.click('#btn-site-apply');
        await rec.pause(300);
      },
      script: 'Paste the link, hit "Analyze" — it reads the role, matches it to Jordan\'s profile, and gets the resume ready. One click on "Apply with AI Agent", and a real browser opens automatically, straight to that job, where it clicks "Apply" itself.',
    },
    {
      id: 'n3-form-fill',
      media: 'clip',
      settleMs: 500,
      record: async (page, rec) => {
        await rec.click('#f-first');
        await rec.type('#f-first', 'Jordan', { delay: 28 });
        await rec.click('#f-last');
        await rec.type('#f-last', 'Lee', { delay: 28 });
        await rec.click('#f-email');
        await rec.type('#f-email', 'jordan.lee@example.com', { delay: 22 });
        await rec.click('#f-phone');
        await rec.type('#f-phone', '(555) 019-2842', { delay: 22 });
        await rec.click('#f-title');
        await rec.type('#f-title', 'Senior Product Manager', { delay: 22 });
        await rec.select('#f-auth', 'citizen');
        await rec.pause(450);
        await rec.click('#btn-submit');
        await rec.pause(400);
      },
      script: "It fills the form from Jordan's profile — name, contact info, title, work authorization — resume attached automatically. Then, \"Submit Application\".",
    },
    {
      id: 'n4-captcha-pause',
      settleMs: 500,
      target: '.captcha-card',
      action: 'none',
      zoom: 1.15,
      script: 'But a verification check stops it cold — Autogram never clicks through: "Human action required. Please complete the verification to continue."',
    },
    {
      id: 'n5-resume-and-summary',
      media: 'clip',
      settleMs: 500,
      record: async (page, rec) => {
        await rec.moveTo('#captcha-check');
        await rec.pause(200);
        await rec.click('#captcha-check');
        await rec.pause(500);
        await rec.skipWhile(async () => {
          await page.waitForSelector('#stage-success.active', { timeout: 10_000 });
        });
        await rec.pause(500);
        await page.evaluate(() => window.showView('view-summary'));
        await rec.pause(600);
      },
      script: 'One click from Jordan clears it, and the application goes through — tracked right back in Autogram: Northbridge Financial, a "92%" match, submitted.',
    },
  ],
};
