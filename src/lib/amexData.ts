// Mock/demo data for the American Express application-flow recreation.
// Every value is fictional or explicitly anonymized — none of this is real
// personal information, and the email/OTP below are not real.

export const amexJob = {
  title: 'Software Engineer I',
  company: 'American Express',
  location: 'Bengaluru, KA, India',
  url: 'careers.americanexpress.com/en/sites/CX_1/jobs/preview/26012390',
  description:
    'Enterprise Architecture is an organization at American Express, and it is a key enabler of the company’s technology strategy.',
  responsibilities: [
    'Designs, develops, tests, and debugs software applications and systems',
    'Completes software builds through consistent development practices',
    'Completes code reviews and automated testing with guidance from peers and leaders',
  ],
} as const;

export const amexProfile = {
  fullName: 'Mehul Agarwal',
  email: 'user@example.com',
  resumeFileName: 'Mehul_Resume.pdf',
} as const;

export const amexAddress = {
  country: 'India',
  addressLine1: 'Demo Address, 4th Cross',
  cityOrTown: 'Bengaluru',
  pinCode: '560001',
  state: 'Karnataka',
} as const;

export const amexExperience = {
  employerName: 'Demo Company',
  jobTitle: 'Software Engineer Intern',
  startMonth: 'Jun',
  startYear: '2023',
  endMonth: 'Aug',
  endYear: '2023',
  responsibilities: 'Built internal tools and automated recurring engineering workflows.',
} as const;

export const amexSkills = ['C++', 'Python', 'React', 'JavaScript'] as const;

export const amexQuestions = [
  {
    question:
      'Do you or your spouse or life partner have an Immediate Family Member or a Close Personal Relationship with anyone who works at obvious competitors of American Express or who provide services to American Express?',
    sensitive: true,
    demoAnswer: 'No' as const,
  },
  {
    question:
      'In the past three years, have you been a partner, principal, shareholder or employee of PricewaterhouseCoopers or any of its affiliated firms?',
    sensitive: true,
    demoAnswer: 'No' as const,
  },
  {
    question:
      'Are you aware of any other information, including personal relationships, concerning PricewaterhouseCoopers, any of its affiliated firms or any of its partners, principals, shareholders or employees that could have the effect of impairing PricewaterhouseCoopers’ independence, either in fact or in appearance?',
    sensitive: false,
    demoAnswer: 'No' as const,
  },
  {
    question:
      'Do you, or your spouse or life partner, have an Immediate Family Member or a Close Personal Relationship with anyone who works at American Express?',
    sensitive: false,
    demoAnswer: 'No' as const,
  },
  {
    question: 'Have you been employed by the American Express subsidiary Accertify within the last three years?',
    sensitive: false,
    demoAnswer: 'No' as const,
  },
  {
    question: 'Do you currently hold or have you held a prominent government position in the last 5 years?',
    sensitive: false,
    demoAnswer: 'No' as const,
  },
] as const;

export const amexVerificationEmail = {
  from: 'American Express Careers',
  fromAddress: 'no-reply@americanexpress-careers.example',
  subject: 'American Express — Verification Code',
  code: '739 402',
  expiry: 'This code expires in 10 minutes.',
} as const;

export const amexTrackerRows = [
  { company: 'American Express', role: 'Software Engineer', status: 'Applied' as const, note: 'Submitted just now' },
  { company: 'Example Technologies', role: 'Backend Engineer', status: 'Applied' as const },
  { company: 'AI Startup', role: 'Software Engineer', status: 'Review' as const },
] as const;
