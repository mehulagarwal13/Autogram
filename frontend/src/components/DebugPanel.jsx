import { useMemo, useState } from "react";
import { Braces, ChevronDown, Copy, ExternalLink, Play, RotateCcw, Server, Timer } from "lucide-react";
import { debugRequest } from "../api";

const EMPTY_BODY = "";

// JSON endpoints that are useful while developing, testing, or diagnosing the
// app. Variable path segments can be edited directly in the request path.
const ENDPOINTS = [
  { group: "System", label: "Health check", method: "GET", path: "/health" },
  { group: "Auth", label: "Current user", method: "GET", path: "/auth/me" },
  { group: "Resumes", label: "List my resumes", method: "GET", path: "/resumes" },
  { group: "Resumes", label: "Extract resume text", method: "POST", path: "/resumes/{resumeId}/extract" },
  { group: "Resumes", label: "Parse resume with AI", method: "POST", path: "/resumes/{resumeId}/parse" },
  { group: "Resumes", label: "Create resume embedding", method: "POST", path: "/resumes/{resumeId}/embed" },
  { group: "Resumes", label: "Preview job shortlist", method: "GET", path: "/resumes/{resumeId}/shortlist?top_n=40" },
  { group: "Matching", label: "Generate matches", method: "POST", path: "/resumes/{resumeId}/matches/generate" },
  { group: "Matching", label: "List matches", method: "GET", path: "/resumes/{resumeId}/matches" },
  { group: "Matching", label: "Update match status", method: "PATCH", path: "/resumes/matches/{matchId}/status", body: '{\n  "status": "saved"\n}' },
  { group: "Jobs", label: "Available job sources", method: "GET", path: "/jobs/sources" },
  { group: "Jobs", label: "Ingest jobs", method: "POST", path: "/jobs/ingest?query=backend%20engineer&sources=all&country=in&results=20" },
  { group: "Jobs", label: "Embed pending jobs", method: "POST", path: "/jobs/embed-pending" },
  { group: "Profile", label: "Get profile", method: "GET", path: "/profile" },
  { group: "Profile", label: "Create profile", method: "POST", path: "/profile", body: '{\n  "full_name": "Your Name",\n  "email": "you@example.com",\n  "location": "Bengaluru, India",\n  "years_of_experience": 2\n}' },
  { group: "Profile", label: "Update profile", method: "PATCH", path: "/profile", body: '{\n  "current_role": "Backend Engineer"\n}' },
  { group: "Profile", label: "List education", method: "GET", path: "/profile/education" },
  { group: "Profile", label: "Add education", method: "POST", path: "/profile/education", body: '{\n  "degree": "B.Tech",\n  "university": "Example University",\n  "field_of_study": "Computer Science"\n}' },
  { group: "Profile", label: "List experience", method: "GET", path: "/profile/experience" },
  { group: "Profile", label: "Add experience", method: "POST", path: "/profile/experience", body: '{\n  "company_name": "Example Corp",\n  "job_title": "Software Engineer",\n  "start_date": "2024-01",\n  "skills_used": ["Python", "FastAPI"]\n}' },
  { group: "Profile", label: "Replace skills", method: "PUT", path: "/profile/skills", body: '{\n  "languages": ["Python"],\n  "frameworks": ["FastAPI"]\n}' },
  { group: "Profile", label: "Get demographics", method: "GET", path: "/profile/demographics" },
  { group: "Profile", label: "List documents", method: "GET", path: "/profile/documents" },
  { group: "Applications", label: "List applications", method: "GET", path: "/applications" },
  { group: "Applications", label: "Start application", method: "POST", path: "/applications/start", body: '{\n  "job_url": "https://boards.greenhouse.io/example/jobs/123",\n  "autopilot_enabled": false,\n  "company": "Example Corp",\n  "position": "Software Engineer"\n}' },
];

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];

function pretty(value) {
  return value === null ? "(empty response)" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export default function DebugPanel({ resume }) {
  const [presetIndex, setPresetIndex] = useState(0);
  const initial = ENDPOINTS[0];
  const [method, setMethod] = useState(initial.method);
  const [path, setPath] = useState(initial.path);
  const [body, setBody] = useState(EMPTY_BODY);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [showHeaders, setShowHeaders] = useState(false);
  const [copyLabel, setCopyLabel] = useState("Copy response");

  const groups = useMemo(() => [...new Set(ENDPOINTS.map((endpoint) => endpoint.group))], []);

  function choosePreset(index) {
    const endpoint = ENDPOINTS[index];
    const resolvedPath = endpoint.path.replace("{resumeId}", resume?.id || "RESUME_ID");
    setPresetIndex(index);
    setMethod(endpoint.method);
    setPath(resolvedPath);
    setBody(endpoint.body || EMPTY_BODY);
    setResult(null);
  }

  async function sendRequest() {
    const normalizedPath = path.trim();
    if (!normalizedPath.startsWith("/")) {
      setResult({ ok: false, status: 0, statusText: "Invalid path", durationMs: 0, headers: {}, body: { error: "Path must start with /." } });
      return;
    }

    let requestBody;
    const hasBody = body.trim().length > 0;
    if (hasBody) {
      try {
        JSON.parse(body);
      } catch (error) {
        setResult({ ok: false, status: 0, statusText: "Invalid JSON", durationMs: 0, headers: {}, body: { error: error.message } });
        return;
      }
      requestBody = body;
    }

    setBusy(true);
    setResult(await debugRequest(normalizedPath, {
      method,
      body: requestBody,
      headers: hasBody ? { "Content-Type": "application/json" } : {},
    }));
    setBusy(false);
  }

  async function copyResponse() {
    if (!result) return;
    await navigator.clipboard.writeText(pretty(result.body));
    setCopyLabel("Copied");
    setTimeout(() => setCopyLabel("Copy response"), 1600);
  }

  const statusClass = result?.ok ? "text-emerald-300 border-emerald-400/25 bg-emerald-500/10" : "text-rose-300 border-rose-400/25 bg-rose-500/10";

  return (
    <section className="animate-fade-up space-y-5" aria-label="API debug console">
      <div className="glass p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="mb-2 flex items-center gap-2 text-indigo-300">
              <Braces size={19} />
              <span className="text-xs font-bold uppercase tracking-[0.16em]">Developer tools</span>
            </div>
            <h2 className="text-xl font-extrabold text-white">Debug API Console</h2>
            <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-400">
              Send authenticated requests through the same frontend proxy and inspect the exact response from the backend.
            </p>
          </div>
          <a href="/api/docs" target="_blank" rel="noreferrer" className="btn-ghost !px-3 !py-2 text-xs">
            Open Swagger docs <ExternalLink size={13} />
          </a>
        </div>

        <div className="mt-6 grid gap-4 lg:grid-cols-[260px_1fr]">
          <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
            API preset
            <select className="input-dark mt-2 normal-case tracking-normal" value={presetIndex} onChange={(event) => choosePreset(Number(event.target.value))}>
              {groups.map((group) => (
                <optgroup key={group} label={group}>
                  {ENDPOINTS.map((endpoint, index) => endpoint.group === group && (
                    <option key={`${endpoint.method}-${endpoint.path}`} value={index}>{endpoint.method} · {endpoint.label}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </label>

          <div className="grid gap-3 sm:grid-cols-[116px_1fr]">
            <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Method
              <select className="input-dark mt-2 font-mono normal-case tracking-normal" value={method} onChange={(event) => setMethod(event.target.value)}>
                {METHODS.map((item) => <option key={item}>{item}</option>)}
              </select>
            </label>
            <label className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              Path
              <input className="input-dark mt-2 font-mono normal-case tracking-normal" value={path} onChange={(event) => setPath(event.target.value)} placeholder="/health" />
            </label>
          </div>
        </div>

        <label className="mt-4 block text-xs font-semibold uppercase tracking-wider text-slate-500">
          JSON request body <span className="normal-case tracking-normal text-slate-600">(optional)</span>
          <textarea className="input-dark mt-2 min-h-40 resize-y font-mono text-xs leading-relaxed" value={body} onChange={(event) => setBody(event.target.value)} placeholder={'{\n  "key": "value"\n}'} spellCheck="false" />
        </label>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button className="btn-primary" onClick={sendRequest} disabled={busy}>
            {busy ? <RotateCcw size={16} className="animate-spin" /> : <Play size={16} />}
            {busy ? "Sending..." : "Send request"}
          </button>
          <button className="btn-ghost !px-3 !py-2 text-xs" onClick={() => choosePreset(presetIndex)} disabled={busy}>
            <RotateCcw size={14} /> Reset preset
          </button>
          <span className="text-xs text-slate-600">Bearer token is added automatically. Upload files from the Dashboard.</span>
        </div>
      </div>

      <div className="glass overflow-hidden">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-white/[0.08] px-5 py-4">
          <div className="flex items-center gap-2">
            <Server size={17} className="text-slate-400" />
            <h3 className="font-bold text-white">Response</h3>
            {result && <span className={`rounded-full border px-2.5 py-0.5 font-mono text-xs font-bold ${statusClass}`}>{result.status || "ERR"} {result.statusText}</span>}
            {result && <span className="flex items-center gap-1 text-xs text-slate-500"><Timer size={12} /> {result.durationMs} ms</span>}
          </div>
          {result && <button className="btn-ghost !px-3 !py-1.5 text-xs" onClick={copyResponse}><Copy size={13} /> {copyLabel}</button>}
        </div>

        {!result ? (
          <div className="flex min-h-64 flex-col items-center justify-center p-8 text-center">
            <Server size={30} className="mb-3 text-slate-700" />
            <p className="font-medium text-slate-500">No request sent yet</p>
            <p className="mt-1 text-xs text-slate-600">Choose an endpoint, edit its data if needed, and send it.</p>
          </div>
        ) : (
          <div className="p-5">
            <pre className="max-h-[420px] overflow-auto rounded-xl border border-white/[0.07] bg-slate-950/70 p-4 font-mono text-xs leading-relaxed text-slate-300">{pretty(result.body)}</pre>
            <button className="mt-3 flex items-center gap-1.5 text-xs text-slate-500 transition hover:text-slate-300" onClick={() => setShowHeaders((visible) => !visible)}>
              Response headers <ChevronDown size={14} className={showHeaders ? "rotate-180" : ""} />
            </button>
            {showHeaders && <pre className="mt-2 max-h-48 overflow-auto rounded-xl border border-white/[0.07] bg-slate-950/50 p-3 font-mono text-[11px] text-slate-400">{pretty(result.headers)}</pre>}
          </div>
        )}
      </div>
    </section>
  );
}
