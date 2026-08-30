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
    const detail = body?.detail;
    // Some endpoints return a STRUCTURED detail object so the UI can branch on
    // a machine-readable reason (e.g. the 409 from starting automation for a
    // job that already has an active run). Previously any non-string detail was
    // JSON.stringify'd straight into the message, which surfaced raw JSON in a
    // toast. Keep `.message` human-readable and hand the object to the caller
    // as `.detail`; existing callers that only read `.message` are unaffected.
    const isStructured = detail && typeof detail === "object";

    // 5xx bodies are NEVER echoed to the user. A 4xx detail is our own
    // deliberate, user-facing prose ("You have already applied to this job"),
    // but a 5xx detail describes a server failure and may carry an exception
    // string or a traceback — from a reverse proxy, or from a route that
    // interpolated an error into `detail`. Autogram's own middleware already
    // returns a generic "Internal server error.", so this is a second line of
    // defence rather than the only one; it costs nothing and removes a whole
    // class of accidental leak.
    const serverFailed = res.status >= 500;
    const message = serverFailed
      ? "Something went wrong on our side. Please try again in a moment."
      : isStructured
        ? (detail.message || `Request failed (${res.status})`)
        : (detail || `Request failed (${res.status})`);
    const error = new Error(message);
    error.status = res.status;
    if (isStructured) error.detail = detail;
    throw error;
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

  listSiteTrustLevels: () => request("/profile/site-trust-levels"),
  setSiteTrustLevel: (domain, trustLevel) =>
    request(`/profile/site-trust-levels/${encodeURIComponent(domain)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ trust_level: trustLevel }),
    }),
  deleteSiteTrustLevel: (domain) =>
    request(`/profile/site-trust-levels/${encodeURIComponent(domain)}`, { method: "DELETE" }),

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

  // ---------- Autonomous agent (general-purpose observe/decide/act) ----------
  startAgentTask: (body) =>
    request("/agent/tasks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  listAgentTasks: () => request("/agent/tasks"),
  getAgentTask: (id) => request(`/agent/tasks/${id}`),
  resumeAgentTask: (id) => request(`/agent/tasks/${id}/resume`, { method: "POST" }),
  answerAgentTask: (id, body) =>
    request(`/agent/tasks/${id}/answer`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  approveAgentTask: (id) => request(`/agent/tasks/${id}/approve`, { method: "POST" }),
  cancelAgentTask: (id) => request(`/agent/tasks/${id}/cancel`, { method: "POST" }),

  // ---------- Human-in-the-loop requests (OTP / MFA / CAPTCHA / login / confirmation) ----------
  getActiveHumanRequest: (taskId) => request(`/agent/tasks/${taskId}/human-request`),
  getHumanRequest: (requestId) => request(`/human-requests/${requestId}`),
  respondHumanRequest: (requestId, body) =>
    request(`/human-requests/${requestId}/respond`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  cancelHumanRequest: (requestId) => request(`/human-requests/${requestId}/cancel`, { method: "POST" }),

  // ---------- Chat transcript ----------
  // `scope` is "applications" or "tasks" — one transcript surface for both
  // automation paths, matching the backend's shared `chat_messages` table.
  getChatTranscript: (scope, id) => request(`/chat/${scope}/${id}`),

  // ---------- Verification code (deterministic path) ----------
  // The code is sent and immediately forgotten: it is not stored in the client,
  // not put in a URL, and the response never echoes it back. See
  // `automation/applications/verification_channel.py` for the server side.
  submitVerificationCode: (applicationId, code) =>
    request(`/applications/${applicationId}/verification-code`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    }),
};

/**
 * Open the live workflow event stream for one automation attempt.
 *
 * The token goes in the QUERY STRING because the browser WebSocket API cannot
 * set an Authorization header — there is no option for it. The backend verifies
 * it with exactly the same logic as every HTTP route.
 *
 * Returns the socket so the caller can close it; `onEvent` receives the parsed
 * payload. KEEPALIVE frames are swallowed here rather than handed upward — they
 * exist only to keep proxies from dropping an idle connection, and every
 * consumer would otherwise have to remember to ignore them.
 *
 * This stream is an ACCELERATOR, never an authority: treat each event as a hint
 * to refetch. Events published while disconnected are dropped and never
 * replayed, so a consumer that trusts the socket as a complete history will be
 * wrong. Fetch the transcript/status on open and after any reconnect.
 */
export function openChatStream(scope, id, onEvent, { onError } = {}) {
  const token = auth.getToken();
  if (!token) return null;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${window.location.host}/api/chat/${scope}/${id}/stream?token=${encodeURIComponent(token)}`;

  let socket;
  try {
    socket = new WebSocket(url);
  } catch {
    onError?.();
    return null;
  }
  socket.onmessage = (raw) => {
    let message;
    try {
      message = JSON.parse(raw.data);
    } catch {
      return; // a frame we cannot parse is not worth tearing the stream down for
    }
    if (message.event === "KEEPALIVE") return;
    onEvent(message);
  };
  socket.onerror = () => onError?.();
  return socket;
}
