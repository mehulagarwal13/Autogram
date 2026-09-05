import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Bot, Loader2, Link2, FileText, ShieldAlert, CheckCircle2,
  XCircle, PlayCircle, ArrowLeft, Send, ThumbsUp, Ban, ExternalLink,
  Paperclip, ArrowRight, Clock3, History,
} from "lucide-react";
import { api } from "../api";
import VerificationModal from "../components/VerificationModal";
import ChatPanel from "../components/ChatPanel";

//: Request types whose response carries a transient secret (a verification
//: code) — these never render as an `InterventionCard`; `VerificationModal`
//: handles them exclusively. Falls back to the task's own `human_intervention`
//: shape either way (`request_type` when the backend normalized it,
//: `type` for older/legacy payloads that predate that field).
const SECRET_REQUEST_TYPES = new Set(["OTP_REQUIRED", "MFA_REQUIRED"]);
function requestTypeOf(intervention) {
  return intervention?.request_type || intervention?.type;
}
function isSecretRequest(intervention) {
  return SECRET_REQUEST_TYPES.has(requestTypeOf(intervention));
}

const POLL_MS = 2500;

const STATUS_STYLES = {
  CREATED: "badge-neutral",
  ANALYZING_JOB: "badge-brand",
  RUNNING: "badge-brand",
  RESUMING: "badge-brand",
  WAITING_FOR_HUMAN: "badge-amber",
  WAITING_FOR_APPROVAL: "badge-blue",
  COMPLETED: "badge-green",
  FAILED: "badge-red",
  CANCELLED: "badge-neutral",
};

const STATUS_LABELS = {
  CREATED: "Created",
  ANALYZING_JOB: "Analyzing Job",
  RUNNING: "Working…",
  RESUMING: "Resuming…",
  WAITING_FOR_HUMAN: "Waiting for You",
  WAITING_FOR_APPROVAL: "Ready to Review",
  COMPLETED: "Completed",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

const ACTIVE = new Set(["CREATED", "ANALYZING_JOB", "RUNNING", "RESUMING", "WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"]);

function StartForm({ toast, onStarted }) {
  const [jobUrl, setJobUrl] = useState("");
  const [resumes, setResumes] = useState([]);
  const [resumeId, setResumeId] = useState("");
  const [busy, setBusy] = useState(false);
  // Set when the backend refuses because this job is already being automated.
  // Kept in state (rather than only toasted) so we can offer a way STRAIGHT to
  // the run that already owns the job — a toast alone leaves the user stuck
  // wondering where it is.
  const [conflict, setConflict] = useState(null);
  const selectedResume = resumes.find((resume) => resume.resume_id === resumeId) || resumes[0];

  useEffect(() => {
    api.listResumes().then((r) => setResumes(r?.resumes || [])).catch(() => {});
  }, []);

  /**
   * `acknowledgement` is passed ONLY by the explicit "Apply Again" action —
   * never on a normal start, and never retained in component state. That
   * matters: keeping it around would turn a deliberate one-time override into
   * a sticky setting that a later ordinary start could silently reuse.
   */
  async function start(acknowledgement = null) {
    if (!jobUrl.trim()) { toast("Paste a job link first.", "error"); return; }
    setBusy(true);
    if (!acknowledgement) setConflict(null);
    try {
      const task = await api.startAgentTask({
        job_url: jobUrl.trim(),
        resume_id: resumeId || null,
        ...(acknowledgement ? { acknowledge_previous_submission: acknowledgement } : {}),
      });
      toast(
        acknowledgement
          ? "Applying again — the agent will open a tab and begin working."
          : "Autonomous agent started — it will open a tab and begin working.",
        "success",
      );
      setConflict(null);
      onStarted(task.task_id);
    } catch (e) {
      // Three distinct conflicts, deliberately not collapsed: something is
      // running now (transient — offer to open it), you already applied
      // (permanent — offer a deliberate re-apply), or the acknowledgement no
      // longer matches (stale — tell them to reload).
      if (e.detail?.reason === "active_automation_exists"
          || e.detail?.reason === "application_already_submitted"
          || e.detail?.reason === "invalid_reapplication_request") {
        setConflict(e.detail);
      } else {
        toast(e.message, "error");
      }
    } finally {
      setBusy(false);
    }
  }

  /** The deliberate override. Echoes back the exact submission being
   *  overridden, which is what the server validates. */
  function applyAgain() {
    if (busy) return;                        // double-click guard
    start({
      path: conflict.path,
      task_id: conflict.task_id ?? null,
      application_id: conflict.application_id ?? null,
    });
  }

  return (
    <div className="card mx-auto max-w-3xl p-6">
      <div className="mb-4 flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-gradient shadow-xs">
          <Bot size={18} className="text-white" />
        </div>
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-brand-600">New agent run</p>
          <h2 className="text-base font-semibold text-slate-900">Launch a supervised application</h2>
          <p className="text-xs text-slate-500">General-purpose: observes the page, decides, acts — no per-site scripting.</p>
        </div>
      </div>

      <label className="mb-1 block text-xs font-medium text-slate-600">Job link</label>
      <div className="relative mb-4">
        <Link2 size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
        <input className="input !pl-9" placeholder="https://company.com/careers/apply/123"
          value={jobUrl} onChange={(e) => setJobUrl(e.target.value)} />
      </div>

      <label className="mb-1 block text-xs font-medium text-slate-600">Resume</label>
      <div className="relative mb-5">
        <FileText size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
        <select className="input !pl-9" value={resumeId} onChange={(e) => setResumeId(e.target.value)}>
          <option value="">Use my most recent resume</option>
          {resumes.map((r) => (
            <option key={r.resume_id} value={r.resume_id}>{r.original_filename}</option>
          ))}
        </select>
      </div>

      {selectedResume ? (
        <div className="mb-5 flex items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50/70 p-3">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-white text-emerald-600 shadow-xs"><FileText size={15} /></span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-xs font-semibold text-slate-800">{selectedResume.original_filename}</p>
            <p className="text-[10px] text-emerald-700">Already selected — the agent will use this for context and browser uploads.</p>
          </div>
          <CheckCircle2 size={15} className="text-emerald-600" />
        </div>
      ) : (
        <div className="mb-5 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          No resume is stored yet. The agent may pause if the application requires one. <Link to="/resumes" className="font-semibold underline">Add a resume</Link>
        </div>
      )}

      {/* `() => start()` deliberately, NOT `onClick={start}`: React would pass
          the click event as the first argument, which `start` would then treat
          as an acknowledgement — a truthy MouseEvent silently sent as
          `acknowledge_previous_submission`. The server would reject it, but a
          normal start must never even look like an override attempt. */}
      <button onClick={() => start()} disabled={busy} className="btn-primary flex w-full items-center justify-center gap-2">
        {busy ? <Loader2 size={15} className="animate-spin" /> : <PlayCircle size={15} />}
        Launch agent
      </button>

      {conflict && (
        <div className="mt-4 rounded-lg border border-amber-200 bg-amber-50 p-3.5">
          <div className="flex items-start gap-2.5">
            <ShieldAlert size={16} className="mt-0.5 shrink-0 text-amber-600" />
            <div className="flex-1">
              <p className="text-sm font-medium text-slate-900">
                {conflict.reason === "application_already_submitted"
                  ? "You've already applied to this job"
                  : "This job is already being automated"}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-slate-600">
                {conflict.message}{" "}
                {conflict.reason === "application_already_submitted"
                  ? "Autogram confirmed the submission, so it won't apply again."
                  : "Autogram won't start a second run on the same job — two automations filling one form at the same time would conflict with each other."}
              </p>
              {conflict.submitted_at && (
                <p className="mt-1 text-xs text-slate-500">
                  Submitted {new Date(conflict.submitted_at).toLocaleString()}
                </p>
              )}
              {conflict.task_id && (
                <button
                  onClick={() => onStarted(conflict.task_id)}
                  className="btn-secondary mt-2.5 flex items-center gap-1.5 text-xs"
                >
                  <ExternalLink size={13} />
                  {conflict.reason === "application_already_submitted"
                    ? "View the completed run"
                    : "Open the run in progress"}
                </button>
              )}
              {conflict.application_id && (
                <Link
                  to={`/applications/${conflict.application_id}`}
                  className="mt-2.5 inline-flex items-center gap-1.5 text-xs font-medium text-brand-600 hover:underline"
                >
                  <ExternalLink size={13} /> View that application
                </Link>
              )}
              {conflict.reason === "active_automation_exists" && (
                <p className="mt-2 text-[11px] text-slate-500">
                  To start over, cancel the run above first — a cancelled or finished run no longer
                  blocks a new attempt.
                </p>
              )}

              {conflict.reason === "invalid_reapplication_request" && (
                <p className="mt-2 text-[11px] text-slate-500">
                  This page is out of date — reload and try again.
                </p>
              )}

              {/* The deliberate re-apply. Only offered for a genuine prior
                  submission, never for an active run (which must be cancelled
                  instead), and never as the default action: the warning comes
                  first and Cancel is the safe, primary choice. */}
              {conflict.reason === "application_already_submitted" && (
                <div className="mt-3 border-t border-amber-200 pt-3">
                  <p className="text-xs font-medium text-slate-800">
                    Applying again may create a duplicate application with the employer.
                  </p>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-600">
                    Only continue if you know this posting was reopened, or you intend to submit a
                    second application.
                  </p>
                  <div className="mt-2.5 flex flex-wrap items-center gap-2">
                    <button
                      onClick={() => setConflict(null)}
                      disabled={busy}
                      className="btn-primary flex items-center gap-1.5 text-xs"
                    >
                      Cancel
                    </button>
                    <button
                      onClick={applyAgain}
                      disabled={busy}
                      className="btn-secondary flex items-center gap-1.5 text-xs !border-amber-300 !text-amber-800"
                    >
                      {busy ? <Loader2 size={13} className="animate-spin" /> : <ShieldAlert size={13} />}
                      Apply again anyway
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      <p className="mt-4 text-xs leading-relaxed text-slate-500">
        The agent will never submit the application without your explicit approval, and will
        pause and ask you whenever it needs a login, CAPTCHA, or information it can't safely
        confirm on its own.
      </p>
      <div className="mt-4 grid gap-2 border-t border-slate-100 pt-4 sm:grid-cols-3">
        {["Resume context locked", "Human checks stay manual", "Approval before submit"].map((label) => (
          <span key={label} className="flex items-center gap-1.5 text-[10px] font-medium text-slate-500">
            <CheckCircle2 size={11} className="text-emerald-500" /> {label}
          </span>
        ))}
      </div>
    </div>
  );
}

//: Per-request-type copy, so the user can tell at a glance WHICH kind of help
//: is wanted. Previously LOGIN / CAPTCHA / MANUAL_ACTION all rendered the
//: single heading "Sign-in or verification needed" with the same
//: "I've handled it" button — actively misleading for a CAPTCHA (nothing to
//: sign into) and for a file the agent needs attached.
//:
//: `browserAction: true` means the fix happens in the open browser TAB and the
//: only response needed is "I did it, continue" (the legacy `/resume` route).
//: Otherwise the user types an answer here (`/answer`). OTP/MFA never reach
//: this component at all — `VerificationModal` owns those.
const INTERVENTION_COPY = {
  LOGIN_REQUIRED: {
    title: "Sign-in required",
    instruction: "Sign in to the site in the browser tab Autogram opened, then continue here. "
      + "Autogram never asks for your password and never types one for you.",
    browserAction: true,
    cta: "I've signed in — continue",
  },
  CAPTCHA_REQUIRED: {
    title: "Security check required",
    instruction: "Complete the “are you human?” challenge yourself in the browser tab Autogram opened, "
      + "then continue here. Autogram will not attempt to solve it.",
    browserAction: true,
    cta: "I've completed it — continue",
  },
  MANUAL_ACTION_REQUIRED: {
    title: "A step needs doing by hand",
    instruction: "Complete this step in the browser tab Autogram opened — for example attaching a file "
      + "Autogram doesn't have a copy of — then continue here.",
    browserAction: true,
    cta: "I've done it — continue",
  },
  USER_CONFIRMATION_REQUIRED: {
    title: "Your confirmation is needed",
    instruction: "Autogram won't take this step without you confirming it first.",
    browserAction: false,
  },
  ANSWER_REQUIRED: {
    title: "Autogram needs an answer",
    instruction: "This application asks something Autogram can't answer from your profile or resume, "
      + "and it won't guess. Your answer is used for this application only.",
    browserAction: false,
  },
  FILE_UPLOAD_REQUIRED: {
    title: "Document required",
    instruction: "Attach the requested document in the conversation below. It becomes available only to this task, then the agent continues from the same page.",
    browserAction: false,
    attachmentAction: true,
  },
  UNKNOWN_BLOCKER: {
    title: "Autogram isn't sure how to continue",
    instruction: "Autogram stopped rather than guess. Check the browser tab it opened — if you clear "
      + "whatever is blocking it, continue here; otherwise tell it what to do.",
    browserAction: true,
    cta: "I've checked — continue",
  },
};

//: Legacy `human_intervention.type` values from before the closed
//: request-type vocabulary existed, so an in-flight task created by an older
//: build still renders sensibly.
const LEGACY_TYPE_ALIASES = {
  authentication: "LOGIN_REQUIRED",
  captcha: "CAPTCHA_REQUIRED",
  sensitive_confirmation: "USER_CONFIRMATION_REQUIRED",
  ambiguous_question: "ANSWER_REQUIRED",
  missing_information: "ANSWER_REQUIRED",
  other: "UNKNOWN_BLOCKER",
};

function interventionCopy(intervention) {
  const raw = requestTypeOf(intervention);
  const key = INTERVENTION_COPY[raw] ? raw : LEGACY_TYPE_ALIASES[raw];
  return (
    INTERVENTION_COPY[key] || {
      title: "Autogram needs your input",
      instruction: "Autogram paused and is waiting on you.",
      browserAction: false,
    }
  );
}

function InterventionCard({ intervention, onResume, onAnswer, busy }) {
  const [answer, setAnswer] = useState("");
  const copy = interventionCopy(intervention);
  const requestType = requestTypeOf(intervention);

  return (
    <div className="card border-l-4 border-l-amber-500 p-5">
      <div className="flex items-start gap-3">
        {copy.browserAction
          ? <ExternalLink size={18} className="mt-0.5 shrink-0 text-amber-600" />
          : <ShieldAlert size={18} className="mt-0.5 shrink-0 text-amber-600" />}
        <div className="flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold text-slate-900">{copy.title}</p>
            {requestType && (
              // The machine-readable type, so a user reporting a problem (or a
              // developer reading over their shoulder) can name it exactly.
              <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500">
                {requestType}
              </span>
            )}
          </div>
          {/* The backend's own message first — it's the specific one. */}
          <p className="mt-1 text-sm text-slate-600">{intervention?.message}</p>
          <p className="mt-1.5 text-xs leading-relaxed text-slate-500">{copy.instruction}</p>
          {intervention?.information_required && (
            <p className="mt-1.5 text-xs text-slate-500">
              Needed: <span className="font-medium text-slate-700">{intervention.information_required}</span>
            </p>
          )}
        </div>
      </div>

      {copy.attachmentAction ? (
        <div className="mt-4 flex items-center gap-2 rounded-lg border border-brand-100 bg-brand-50 px-3 py-2.5 text-xs text-brand-800">
          <Paperclip size={14} /> Use the secure attachment control in the conversation below.
        </div>
      ) : copy.browserAction ? (
        <div className="mt-4">
          <button onClick={onResume} disabled={busy} className="btn-primary flex items-center gap-2">
            {busy ? <Loader2 size={14} className="animate-spin" /> : <PlayCircle size={14} />}
            {copy.cta || "I've handled it — continue"}
          </button>
        </div>
      ) : (
        <div className="mt-4 flex flex-col gap-2 sm:flex-row">
          <input className="input flex-1" placeholder="Your answer..."
            value={answer} onChange={(e) => setAnswer(e.target.value)} />
          <button
            onClick={() => onAnswer(intervention?.information_required || intervention?.reason || "question", answer)}
            disabled={busy || !answer.trim()}
            className="btn-primary flex items-center justify-center gap-2 whitespace-nowrap"
          >
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            Submit answer
          </button>
        </div>
      )}
    </div>
  );
}

function ReadyForSubmissionCard({ task, onApprove, onCancel, busy }) {
  return (
    <div className="card border-l-4 border-l-brand-500 p-5">
      <div className="flex items-start gap-3">
        <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-brand-600" />
        <div className="flex-1">
          <p className="text-sm font-semibold text-slate-900">Application ready for submission</p>
          <p className="mt-1 text-sm text-slate-600">
            The agent has completed every step it could and stopped before the final submit —
            review the evidence below, then approve to let it click Submit, or cancel.
          </p>
          {task.final_result?.evidence && (
            <p className="mt-2 rounded-lg bg-slate-50 p-3 text-xs text-slate-600">{task.final_result.evidence}</p>
          )}
        </div>
      </div>
      <div className="mt-4 flex gap-2">
        <button onClick={onApprove} disabled={busy} className="btn-primary flex items-center gap-2">
          {busy ? <Loader2 size={14} className="animate-spin" /> : <ThumbsUp size={14} />}
          Approve &amp; Submit
        </button>
        <button onClick={onCancel} disabled={busy} className="btn-secondary flex items-center gap-2">
          <Ban size={14} /> Cancel
        </button>
      </div>
    </div>
  );
}

function ActionChecklist({ actionHistory }) {
  if (!actionHistory?.length) {
    return <p className="text-xs text-slate-400">No actions taken yet.</p>;
  }
  return (
    <ul className="space-y-1.5">
      {actionHistory.slice(-25).map((a, i) => (
        <li key={i} className="flex items-start gap-2 text-xs">
          {a.success ? (
            <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-emerald-500" />
          ) : (
            <XCircle size={13} className="mt-0.5 shrink-0 text-red-400" />
          )}
          <span className="text-slate-600">
            <span className="font-medium text-slate-700">{a.action_type}</span>
            {a.element_name ? ` — ${a.element_name}` : ""}
            {a.detail ? `: ${a.detail}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

//: The right rail of the run workspace — what the agent is working with and
//: everything it has done — sitting alongside the conversation rather than
//: stacked under it. Progress prefers the executor's own field tally
//: (`application_progress`, when populated) and otherwise falls back to a
//: verified-action count so the card is never blank on an in-flight run.
function RunSidebar({ task }) {
  const actions = task.action_history || [];
  const verified = actions.filter((a) => a.success).length;
  const documentCount = (task.uploaded_documents || []).length;
  const { completed_fields: done, total_fields: total } = task.application_progress || {};
  const hasFieldProgress = Number.isFinite(done) && Number.isFinite(total) && total > 0;
  const pct = hasFieldProgress ? Math.min(100, Math.round((done / total) * 100)) : 0;

  return (
    <aside className="space-y-4">
      <div className="card p-5">
        <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Progress</p>
        {hasFieldProgress ? (
          <>
            <div className="mt-3 flex items-baseline justify-between">
              <span className="text-2xl font-semibold text-slate-900">
                {done}<span className="text-sm font-normal text-slate-400">/{total}</span>
              </span>
              <span className="text-xs text-slate-500">fields completed</span>
            </div>
            <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
              <div className="h-full rounded-full bg-brand-gradient transition-all" style={{ width: `${pct}%` }} />
            </div>
          </>
        ) : (
          <p className="mt-3 flex items-baseline gap-1.5">
            <span className="text-2xl font-semibold text-slate-900">{verified}</span>
            <span className="text-xs text-slate-500">verified action{verified === 1 ? "" : "s"} so far</span>
          </p>
        )}
        <dl className="mt-4 space-y-1.5 border-t border-slate-100 pt-3 text-xs">
          <div className="flex items-center justify-between">
            <dt className="text-slate-500">Steps taken</dt>
            <dd className="font-medium text-slate-700">{actions.length}</dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-slate-500">Documents available</dt>
            <dd className="font-medium text-slate-700">{documentCount}</dd>
          </div>
        </dl>
      </div>

      <div className="card p-5">
        <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-slate-500">Activity</p>
        <div className="max-h-[420px] overflow-y-auto pr-1">
          <ActionChecklist actionHistory={task.action_history} />
        </div>
      </div>
    </aside>
  );
}

function TaskView({ taskId, toast, onBack }) {
  const [task, setTask] = useState(null);
  const [busy, setBusy] = useState(false);
  const [verificationError, setVerificationError] = useState(null);
  const pollRef = useRef(null);

  const load = useCallback(async () => {
    try {
      const t = await api.getAgentTask(taskId);
      setTask(t);
    } catch (e) {
      toast(e.message, "error");
    }
  }, [taskId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    load();
    pollRef.current = setInterval(load, POLL_MS);
    return () => clearInterval(pollRef.current);
  }, [load]);

  // A rejected code raises a brand-new HumanInteractionRequest (new
  // request_id) — clear any stale error message from the PREVIOUS request
  // rather than letting it linger under the fresh verification modal.
  useEffect(() => {
    setVerificationError(null);
  }, [task?.human_intervention?.request_id]);

  async function withBusy(fn) {
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function submitVerificationCode(code) {
    const hi = task.human_intervention;
    setVerificationError(null);
    setBusy(true);
    try {
      await api.respondHumanRequest(hi.request_id, {
        action: requestTypeOf(hi) === "MFA_REQUIRED" ? "MFA_SUBMITTED" : "OTP_SUBMITTED",
        value: code,
      });
      await load();
    } catch (e) {
      setVerificationError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancelVerification() {
    const hi = task.human_intervention;
    setBusy(true);
    try {
      await api.cancelHumanRequest(hi.request_id);
      await load();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function attachAndContinue(file, documentType) {
    setBusy(true);
    try {
      await api.attachAgentTaskDocument(taskId, file, documentType);
      toast(`${file.name} is now available to this task.`, "success");
      await load();
    } catch (error) {
      throw error;
    } finally {
      setBusy(false);
    }
  }

  if (!task) {
    return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;
  }

  const status = task.current_status;
  let jobHost = task.job_url;
  try { jobHost = new URL(task.job_url).hostname.replace(/^www\./, ""); } catch { /* show the original URL */ }

  return (
    <div className="space-y-5">
      <button onClick={onBack} className="flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-slate-900">
        <ArrowLeft size={14} /> All agent runs
      </button>

      <div className="card overflow-hidden">
        <div className="border-b border-slate-100 bg-slate-50/70 px-5 py-2.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">
          Live application workspace · {jobHost}
        </div>
        <div className="p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="text-xs text-slate-500">Goal</p>
            <p className="text-sm font-medium text-slate-900">{task.original_objective}</p>
            <a href={task.job_url} target="_blank" rel="noreferrer" className="mt-1 inline-block text-xs text-brand-600 hover:underline">
              {task.job_url}
            </a>
          </div>
          <span className={`badge ${STATUS_STYLES[status] || "badge-neutral"}`}>{STATUS_LABELS[status] || status}</span>
        </div>
        {ACTIVE.has(status) && !["WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"].includes(status) && (
          <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
            <Loader2 size={13} className="animate-spin text-brand-600" />
            Watching the page and deciding the next step…
          </div>
        )}
        {task.error && <p className="mt-3 text-xs text-red-600">{task.error}</p>}
        </div>
      </div>

      {status === "WAITING_FOR_HUMAN" && task.human_intervention && isSecretRequest(task.human_intervention) && (
        <VerificationModal
          // Forces a full remount whenever the ACTIVE request changes (a
          // rejected code raises a brand-new request_id) — otherwise React
          // would reuse this component instance across two different
          // requests, and a partially-typed code (or a stale countdown)
          // from the OLD request could linger into the new one.
          key={task.human_intervention.request_id}
          request={task.human_intervention}
          submitting={busy}
          error={verificationError}
          onSubmit={submitVerificationCode}
          onCancel={cancelVerification}
        />
      )}

      {/* Operations workspace: the conversation and whatever the agent needs
          from a human on the left; persistent run context and the audit trail
          on the right, where they stay visible as the transcript scrolls. */}
      <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-5">
          {status === "WAITING_FOR_HUMAN" && task.human_intervention
            && !isSecretRequest(task.human_intervention)
            && !["ANSWER_REQUIRED", "FILE_UPLOAD_REQUIRED"].includes(requestTypeOf(task.human_intervention)) && (
            <InterventionCard
              intervention={task.human_intervention}
              busy={busy}
              onResume={() => withBusy(() => api.resumeAgentTask(taskId))}
              onAnswer={(question, answer) => withBusy(() => api.answerAgentTask(taskId, { question, answer }))}
            />
          )}

          {/* The conversation for this task. Rendered for every status, not just
              while paused: the transcript is the user's history of what the
              automation did and asked, and hiding it once the task resumes would
              make the one durable explanation of a run disappear exactly when
              someone wants to look back at it. */}
          <ChatPanel
            scope="tasks"
            resourceId={taskId}
            activeRequest={
              status === "WAITING_FOR_HUMAN" && task.human_intervention && !isSecretRequest(task.human_intervention)
                ? task.human_intervention
                : null
            }
            busy={busy}
            documents={task.uploaded_documents || []}
            onAttach={attachAndContinue}
            onRespond={(answer) =>
              withBusy(() =>
                api.answerAgentTask(taskId, {
                  question: task.human_intervention?.message || "Autogram asked for more information.",
                  answer,
                }),
              )
            }
          />

          {status === "WAITING_FOR_APPROVAL" && (
            <ReadyForSubmissionCard
              task={task}
              busy={busy}
              onApprove={() => withBusy(() => api.approveAgentTask(taskId))}
              onCancel={() => withBusy(() => api.cancelAgentTask(taskId))}
            />
          )}

          {status === "COMPLETED" && (
            <div className="card border-l-4 border-l-emerald-500 p-5">
              <p className="text-sm font-semibold text-slate-900">Application submitted</p>
              <p className="mt-1 text-sm text-slate-600">{task.final_result?.evidence}</p>
            </div>
          )}

          {status === "FAILED" && (
            <div className="card border-l-4 border-l-red-500 p-5">
              <p className="text-sm font-semibold text-slate-900">The agent could not finish this task</p>
              <p className="mt-1 text-sm text-slate-600">{task.error || task.final_result?.evidence}</p>
            </div>
          )}

          {!["COMPLETED", "FAILED", "CANCELLED"].includes(status) && status !== "WAITING_FOR_APPROVAL" && (
            <div className="flex justify-end">
              <button onClick={() => withBusy(() => api.cancelAgentTask(taskId))} disabled={busy}
                className="btn-secondary flex items-center gap-2 text-xs">
                <Ban size={13} /> Cancel task
              </button>
            </div>
          )}
        </div>

        <RunSidebar task={task} />
      </div>
    </div>
  );
}

function RecentAgentRuns({ toast }) {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.listAgentTasks()
      .then((items) => setTasks((items || []).slice(0, 6)))
      .catch((error) => toast(error.message, "error"))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!loading && tasks.length === 0) return null;

  return (
    <section className="mx-auto max-w-3xl">
      <div className="mb-3 flex items-center justify-between">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900"><History size={15} /> Recent agent runs</h2>
          <p className="mt-0.5 text-[11px] text-slate-500">Resume any active application or review its audit trail.</p>
        </div>
      </div>
      <div className="card divide-y divide-slate-100 overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-xs text-slate-500"><Loader2 size={14} className="mr-2 animate-spin" />Loading runs...</div>
        ) : tasks.map((task) => {
          let host = task.job_url;
          try { host = new URL(task.job_url).hostname.replace(/^www\./, ""); } catch { /* keep raw value */ }
          const needsAttention = ["WAITING_FOR_HUMAN", "WAITING_FOR_APPROVAL"].includes(task.current_status);
          return (
            <Link key={task.task_id} to={`/agent/${task.task_id}`} className="group flex items-center gap-3 px-5 py-3.5 transition-colors hover:bg-slate-50">
              <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${needsAttention ? "bg-amber-50 text-amber-600" : "bg-brand-50 text-brand-600"}`}>
                {needsAttention ? <Clock3 size={16} /> : <Bot size={16} />}
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-semibold text-slate-800">{host}</p>
                <p className="mt-0.5 text-[11px] text-slate-500">{task.action_history?.length || 0} verified actions</p>
              </div>
              <span className={`badge ${STATUS_STYLES[task.current_status] || "badge-neutral"}`}>{STATUS_LABELS[task.current_status] || task.current_status}</span>
              <ArrowRight size={14} className="text-slate-300 transition-transform group-hover:translate-x-0.5 group-hover:text-brand-500" />
            </Link>
          );
        })}
      </div>
    </section>
  );
}

export default function AutonomousAgent({ toast }) {
  const { id } = useParams();
  const navigate = useNavigate();

  if (id) {
    return (
      <div className="animate-fade-up">
        <TaskView taskId={id} toast={toast} onBack={() => navigate("/agent")} />
      </div>
    );
  }

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <p className="page-kicker">A helping hand</p>
        <h1 className="page-title">Your application assistant</h1>
        <p className="page-subtitle">
          Paste a job link and let the agent observe, decide, and act through the whole application —
          pausing for logins, CAPTCHAs, and anything it can't safely answer on its own.
        </p>
      </div>
      <StartForm toast={toast} onStarted={(taskId) => navigate(`/agent/${taskId}`)} />
      <RecentAgentRuns toast={toast} />
    </div>
  );
}
