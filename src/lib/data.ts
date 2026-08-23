// Mock/demo data only. Nothing here is a real credential, real OTP, or real
// personal information — every value exists purely to make the UI mockups
// legible on screen.

export const demoProfile = {
  name: 'Mehul Agarwal',
  firstName: 'Mehul',
  lastName: 'Agarwal',
  email: 'mehul.demo@example.com',
  phone: '+91 98•••••210',
  location: 'India',
  university: 'Jaypee Institute of Information Technology',
  degree: 'Bachelor of Technology',
  skills: ['C++', 'Python', 'React', 'JavaScript'],
  experienceYears: '2+ years',
  linkedin: 'linkedin.com/in/mehul-demo',
  github: 'github.com/mehul-demo',
  portfolio: 'mehul-demo.dev',
  workAuthorization: 'Authorized to work in India',
  resumeFileName: 'Mehul_Resume.pdf',
} as const;

export const profileExtractionFields = [
  { label: 'Name', value: demoProfile.name },
  { label: 'Email', value: demoProfile.email },
  { label: 'Phone', value: demoProfile.phone },
  { label: 'Location', value: demoProfile.location },
  { label: 'University', value: demoProfile.university },
  { label: 'Degree', value: demoProfile.degree },
  { label: 'Skills', value: demoProfile.skills.join(', ') },
  { label: 'Experience', value: `${demoProfile.experienceYears} — 2 projects` },
  { label: 'LinkedIn', value: demoProfile.linkedin },
  { label: 'GitHub', value: demoProfile.github },
  { label: 'Portfolio', value: demoProfile.portfolio },
  { label: 'Work Authorization', value: demoProfile.workAuthorization },
] as const;

export const demoJob = {
  title: 'Software Engineer — AI Platform',
  company: 'Example Technologies',
  location: 'Bengaluru, India · Hybrid',
  type: 'Full-time',
  url: 'careers.exampletech.com/jobs/software-engineer-ai-platform',
  description:
    'We are looking for a Software Engineer to help build the next generation of our AI platform. You will work across the stack, from data pipelines to production services.',
} as const;

export const applicationFormFields = [
  { label: 'First Name', value: demoProfile.firstName },
  { label: 'Last Name', value: demoProfile.lastName },
  { label: 'Email', value: demoProfile.email },
  { label: 'Phone', value: demoProfile.phone },
  { label: 'Location', value: demoProfile.location },
  { label: 'University', value: demoProfile.university },
  { label: 'Degree', value: demoProfile.degree },
  { label: 'Skills', value: demoProfile.skills.join(', ') },
] as const;

export const screeningQuestions = [
  {
    question: 'What is your highest level of education?',
    answer: demoProfile.degree,
    source: 'Matched from Education',
  },
  {
    question: 'Are you legally authorized to work in this country?',
    answer: 'Yes',
    source: 'Matched from Work Authorization',
  },
  {
    question: 'How many years of professional experience do you have?',
    answer: demoProfile.experienceYears,
    source: 'Matched from Experience',
  },
] as const;

export const processingSteps = [
  'Analyzing job posting...',
  'Detecting application platform...',
  'Understanding application fields...',
  'Matching your profile...',
  'Preparing application...',
] as const;

export const trackerRows = [
  { company: 'Example Technologies', role: 'Software Engineer', status: 'Applied' as const },
  { company: 'AI Startup', role: 'Backend Engineer', status: 'Applied' as const },
  { company: 'FinTech Corp', role: 'Software Engineer', status: 'Applied' as const },
  { company: 'CloudNova', role: 'Platform Engineer', status: 'Needs OTP' as const },
  { company: 'DataForge Labs', role: 'ML Engineer', status: 'Needs CAPTCHA' as const },
] as const;

export const pipelineStages = [
  'Job Link',
  'Autogram AI',
  'Understand Application',
  'Fill Form',
  'Human Verification',
  'Review',
  'Submit',
  'Track Application',
] as const;

export const verificationEmail = {
  from: 'Example Technologies Careers',
  fromAddress: 'no-reply@exampletech-careers.example',
  subject: 'Your verification code',
  code: '482 913',
  expiry: 'This code expires in 10 minutes.',
} as const;
