// Thin API client. The Vite dev server proxies /api/* to FastAPI (see vite.config.js).
// Auth: JWT bearer token, persisted in localStorage, attached to every request.

const TOKEN_KEY = "ajagent_token";

export const auth = {
  getToken: () => localStorage.getItem(TOKEN_KEY),
  setToken: (t) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = auth.getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const res = await fetch(`/api${path}`, { ...options, headers });
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON response */
  }
  if (res.status === 401 && !path.startsWith("/auth/")) {
    auth.clear();
    onUnauthorized?.();
    throw new Error("Session expired — please log in again.");
  }
  if (!res.ok) {
    const detail = body?.detail || `Request failed (${res.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

export const api = {
  health: () => request("/health"),

  // Auth
  signup: (email, password) =>
    request("/auth/signup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  login: (email, password) =>
    request("/auth/login", {
      method: "POST",
      // OAuth2 password flow: form-encoded, email travels as `username`
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: email, password }),
    }),
  me: () => request("/auth/me"),

  // Resume pipeline
  listResumes: () => request("/resumes"),
  uploadResume: (file) => {
    const form = new FormData();
    form.append("file", file);
    return request("/resumes/upload", { method: "POST", body: form });
  },
  extract: (resumeId) => request(`/resumes/${resumeId}/extract`, { method: "POST" }),
  parse: (resumeId) => request(`/resumes/${resumeId}/parse`, { method: "POST" }),
  embed: (resumeId) => request(`/resumes/${resumeId}/embed`, { method: "POST" }),

  // Jobs
  ingestJobs: ({ query, sources, country, location, results }) => {
    const params = new URLSearchParams({ query, sources, country, results });
    if (location) params.set("location", location);
    return request(`/jobs/ingest?${params}`, { method: "POST" });
  },
  embedPending: () => request("/jobs/embed-pending", { method: "POST" }),

  // Matching
  generateMatches: (resumeId, { location, minSalary } = {}) => {
    const params = new URLSearchParams();
    if (location) params.set("location_contains", location);
    if (minSalary) params.set("min_salary", minSalary);
    const qs = params.toString();
    return request(`/resumes/${resumeId}/matches/generate${qs ? `?${qs}` : ""}`, { method: "POST" });
  },
  listMatches: (resumeId, status) => {
    const qs = status ? `?status=${status}` : "";
    return request(`/resumes/${resumeId}/matches${qs}`);
  },
  setMatchStatus: (matchId, status) =>
    request(`/resumes/matches/${matchId}/status`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    }),

  // ---------- Profile (master candidate profile) ----------
  getProfile: () => request("/profile"),
  createProfile: (body) =>
    request("/profile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  updateProfile: (body) =>
    request("/profile", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  setSkills: (body) =>
    request("/profile/skills", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  updateAutomationSettings: (body) =>
    request("/profile/automation-settings", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),

  listEducation: () => request("/profile/education"),
  addEducation: (body) =>
    request("/profile/education", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  updateEducation: (id, body) =>
    request(`/profile/education/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  deleteEducation: (id) => request(`/profile/education/${id}`, { method: "DELETE" }),

  listExperience: () => request("/profile/experience"),
  addExperience: (body) =>
    request("/profile/experience", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  updateExperience: (id, body) =>
    request(`/profile/experience/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  deleteExperience: (id) => request(`/profile/experience/${id}`, { method: "DELETE" }),

  listDocuments: (documentType) => request(`/profile/documents${documentType ? `?document_type=${documentType}` : ""}`),
  uploadDocument: (file, { documentType, label, jobTypeTag } = {}) => {
    const form = new FormData();
    form.append("file", file);
    const params = new URLSearchParams({ document_type: documentType || "resume" });
    if (label) params.set("label", label);
    if (jobTypeTag) params.set("job_type_tag", jobTypeTag);
    return request(`/profile/documents/upload?${params}`, { method: "POST", body: form });
  },
  setDefaultDocument: (id) => request(`/profile/documents/${id}/set-default`, { method: "PATCH" }),
  deleteDocument: (id) => request(`/profile/documents/${id}`, { method: "DELETE" }),

  getDemographics: () => request("/profile/demographics"),
  putDemographics: (body) =>
    request("/profile/demographics", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),

  // ---------- Applications (auto-apply / HITL platform) ----------
  startApplication: (body) =>
    request("/applications/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  listApplications: () => request("/applications"),
  getApplication: (id) => request(`/applications/${id}`),
  listApplicationRuns: (id) => request(`/applications/${id}/runs`),
  getApplicationsOverview: () => request("/applications/overview"),
  listApplicationReviews: () => request("/applications/reviews"),
  checkDuplicateApplication: ({ company, position }) => {
    const params = new URLSearchParams();
    if (company) params.set("company", company);
    if (position) params.set("position", position);
    return request(`/applications/check-duplicate?${params}`);
  },
  getApplicationLive: (id) => request(`/applications/${id}/live`),
  listApplicationQuestions: (id) => request(`/applications/${id}/questions`),
  getApplicationReviewSummary: (id) => request(`/applications/${id}/review-summary`),
  listApplicationAuditLog: (id) => request(`/applications/${id}/audit-log`),
  reviewQuestion: (applicationId, questionId, body) =>
    request(`/applications/${applicationId}/questions/${questionId}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),
  approveApplication: (id) => request(`/applications/${id}/approve`, { method: "POST" }),
  rejectApplication: (id, reason) =>
    request(`/applications/${id}/reject${reason ? `?reason=${encodeURIComponent(reason)}` : ""}`, { method: "POST" }),
};
