// Autogram — "Auto Apply Agent" future-vision demo.
// This flow captures a purpose-built static mockup (../../../../future-vision-demo/mockup/index.html
// inside the Autogram repo), NOT the real Autogram app/backend/extension. The mockup is an
// interactive, real-DOM prototype of an imagined future feature, styled to match Autogram's
// actual dark glassmorphism design system (indigo/fuchsia gradient, Inter font, glass cards).
// No server, no login, no real app was run to produce this video.

// The mockup lives in the Autogram repo (a different drive than this
// Ultrademo workspace), so an absolute path is used rather than a relative one.
const BASE = 'file:///D:/PROJECT/autogram/Autogram/future-vision-demo/mockup/index.html';

export default {
  title: 'Autogram — Auto Apply Agent (Future Vision)',
  template: 'sizzle',
  colorScheme: 'dark',
  brand: '#6366f1',

  caption: { theme: 'pill', size: 'md', position: 'bottom' },

  intro: {
    title: 'Autogram',
    subtitle: 'The future of the Auto Apply Agent',
    script: "Hey — quick look at where Autogram is headed. Today, it already reads your resume and matches you to real jobs. Here's the vision: an \"Auto Apply Agent\" that searches, evaluates, and applies on your behalf, while every verification step still comes back to you. Let's walk through it.",
  },
  outro: {
    title: 'The future of Autogram',
    subtitle: 'AI does the searching and applying. You stay in control.',
    script: "That's the vision — Autogram doing the repetitive work, and you making the calls that matter, like verification and final approval. We're building toward this next.",
  },

  scenes: [
    {
      id: 's1-dashboard',
      prepare: async (page) => {
        await page.goto(BASE, { waitUntil: 'domcontentloaded' });
      },
      settleMs: 800,
      target: '#agent-card',
      action: 'none',
      zoom: 1.12,
      script: "Okay, so here's Jordan's dashboard — Autogram already knows their skills, their experience, and their resume, because it was parsed and embedded automatically. Now let's turn on the \"Auto Apply Agent\" and see what it actually does.",
    },
    {
      id: 's2-toggle-on',
      media: 'clip',
      settleMs: 500,
      record: async (page, rec) => {
        await rec.moveTo('#toggle-auto-apply');
        await rec.pause(300);
        await rec.click('#toggle-auto-apply');
        await rec.pause(1400);
        await rec.skipWhile(async () => {
          await page.waitForSelector('#view-automation.active', { timeout: 15_000 });
        });
        await rec.pause(1400);
      },
      script: "Watch this — the moment I flip that toggle \"ON\", a real automation browser spins up, and the agent starts searching job boards on its own.",
    },
    {
      id: 's3-search-results',
      settleMs: 600,
      target: '#job-card-1',
      zoom: 1.25,
      script: "It's scanning TalentHub for senior product manager roles — and this one at Nimbus Systems already stands out, so it opens it.",
    },
    {
      id: 's4-job-detail',
      settleMs: 700,
      target: '.jd-body',
      action: 'none',
      zoom: 1.2,
      script: 'Before doing anything else, it actually reads the description — pulling out the exact skills that matter: "A/B tests", "SQL", "stakeholders", "roadmapping".',
    },
    {
      id: 's5-match-score',
      settleMs: 500,
      target: '#btn-proceed',
      zoom: 1.18,
      script: 'And here\'s the payoff — a clear AI match score, "94%", with the reasoning right there: six out of six required skills, pay in range, remote, experience covered. So it goes ahead and clicks "Proceed with Application".',
    },
    {
      id: 's6-form-fill',
      media: 'clip',
      settleMs: 600,
      record: async (page, rec) => {
        await rec.click('#f-first');
        await rec.type('#f-first', 'Jordan');
        await rec.click('#f-last');
        await rec.type('#f-last', 'Lee');
        await rec.click('#f-email');
        await rec.type('#f-email', 'jordan.lee@example.com');
        await rec.click('#f-phone');
        await rec.type('#f-phone', '(555) 019-2842');
        await rec.click('#f-years');
        await rec.type('#f-years', '8');
        await rec.select('#f-auth', 'citizen');
        await rec.pause(900); // resume auto-attaches here
        await rec.click('#f-cover');
        await rec.type('#f-cover', 'My product and analytics background lines up closely with this role.');
        await rec.pause(400);
        await rec.click('#btn-submit');
        await rec.pause(800);
      },
      script: "Now here's the part people always worry about — the actual application form. It fills in Jordan's name, contact details, and years of experience straight from their profile, picks the right work authorization option, and notice the resume just attaches on its own, pulled from what was uploaded earlier. It even drafts a short line on why Jordan's a fit, and then hits \"Submit Application\".",
    },
    {
      id: 's7-captcha-pause',
      settleMs: 600,
      target: '.captcha-card',
      action: 'none',
      zoom: 1.15,
      script: 'But right here, it stops. A verification check pops up, and Autogram will not click through that for you — it pauses and waits: "Human action required. Please complete the verification to continue."',
    },
    {
      id: 's8-resume',
      media: 'clip',
      settleMs: 500,
      record: async (page, rec) => {
        await rec.moveTo('#captcha-check');
        await rec.pause(300);
        await rec.click('#captcha-check');
        await rec.pause(700);
        await rec.skipWhile(async () => {
          await page.waitForSelector('#stage-success.active', { timeout: 15_000 });
        });
        await rec.pause(1000);
      },
      script: "Jordan just checks the box themselves — that one click is all it takes — and the moment it's verified, the agent picks right back up and finishes submitting the application on its own.",
    },
    {
      id: 's9-analytics',
      prepare: async (page) => {
        await page.evaluate(() => window.showView('view-analytics'));
      },
      settleMs: 900,
      target: '.an-grid',
      action: 'none',
      zoom: 1.12,
      script: "Zoom out to the dashboard, and here's the full picture: 128 jobs discovered, 23 applications submitted, a handful waiting on your approval, and three interview opportunities already lined up — everything Autogram found, evaluated, and acted on, with you still calling the shots.",
    },
  ],
};
