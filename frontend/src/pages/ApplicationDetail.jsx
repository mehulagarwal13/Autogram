import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeft, Building2, ExternalLink, Loader2, AlertTriangle, ShieldAlert,
  CheckCircle2, RotateCcw, ChevronDown, FileText,
} from "lucide-react";
import { api } from "../api";
import StatusBadge from "../components/StatusBadge";
import AnswerReviewList from "../components/AnswerReviewList";

const LIVE_POLL_MS = 2500;
const ACTIVE_STATUSES = new Set(["IN_PROGRESS", "WAITING_FOR_HUMAN"]);

export default function ApplicationDetail({ toast }) {
  const { id } = useParams();
  const navigate = useNavigate();

  const [application, setApplication] = useState(null);
  const [live, setLive] = useState(null);
  const [questions, setQuestions] = useState([]);
  const [summary, setSummary] = useState(null);
  const [runs, setRuns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [expandedRun, setExpandedRun] = useState(null);
  const pollRef = useRef(null);

  const loadAll = useCallback(async () => {
    try {
      const [app, qs, sum, rns] = await Promise.all([
        api.getApplication(id),
        api.listApplicationQuestions(id),
        api.getApplicationReviewSummary(id),
        api.listApplicationRuns(id),
      ]);
      setApplication(app);
      setQuestions(qs);
      setSummary(sum);
      setRuns(rns);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setLoading(false);
    }
  }, [id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { loadAll(); }, [loadAll]);

  // Live polling — only while something is actually active, so the dashboard
  // doesn't keep hammering the API for an application that finished hours ago.
  useEffect(() => {
    async function tick() {
      try {
        const l = await api.getApplicationLive(id);
        setLive(l);
        if (ACTIVE_STATUSES.has(l.display_status) || l.live) {
          // Refresh the full record occasionally so status transitions
          // (e.g. WAITING_FOR_HUMAN -> IN_PROGRESS -> READY_TO_SUBMIT) show up
          // without a manual reload.
          const app = await api.getApplication(id);
          setApplication(app);
        }
      } catch {
        /* transient poll failure — try again next tick */
      }
    }
    tick();
    pollRef.current = setInterval(tick, LIVE_POLL_MS);
    return () => clearInterval(pollRef.current);
  }, [id]);

  async function reviewQuestion(questionId, action, answer) {
    try {
      const updated = await api.reviewQuestion(id, questionId, { action, answer });
      setQuestions((qs) => qs.map((q) => (q.question_id === questionId ? updated : q)));
      const sum = await api.getApplicationReviewSummary(id);
      setSummary(sum);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function approve() {
    setBusy(true);
    try {
      const result = await api.approveApplication(id);
      toast(result.message, result.status === "applied" ? "success" : "info");
      await loadAll();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!window.confirm("Go back and edit — this cancels the current application attempt. Continue?")) return;
    setBusy(true);
    try {
      await api.rejectApplication(id, "Rejected from review screen");
      toast("Application cancelled.", "info");
      await loadAll();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  async function retry() {
    if (!application) return;
    setBusy(true);
    try {
      await api.startApplication({
        job_url: application.job_url, company: application.company, position: application.position,
      });
      toast("Application requeued.", "success");
      await loadAll();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  if (loading || !application) {
    return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;
  }

  const status = application.display_status;
  const isWaitingLive = live?.live?.status === "WAITING_FOR_HUMAN";
  const isWaitingTimedOut = status === "WAITING_FOR_HUMAN" && !isWaitingLive;
  const isActive = status === "IN_PROGRESS" || isWaitingLive;
  const lowConfidenceQuestions = questions.filter((q) => q.confidence_level === "LOW" && q.review_status === "pending_review");

  return (
    <div className="animate-fade-up space-y-5">
      <button onClick={() => navigate("/applications")} className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-800">
        <ArrowLeft size={14} /> Back to applications
      </button>

      {/* Job info header */}
      <div className="card p-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold text-slate-900">{application.position || "Application"}</h1>
            <p className="mt-1 flex items-center gap-1.5 text-sm text-slate-500">
              <Building2 size={14} /> {application.company || "Unknown company"}
              {application.ats_platform && <span className="badge badge-neutral">{application.ats_platform}</span>}
            </p>
          </div>
          <StatusBadge status={status} />
        </div>
        <a href={application.job_url} target="_blank" rel="noreferrer"
          className="mt-3 inline-flex items-center gap-1 text-xs text-brand-600 hover:underline">
          View original posting <ExternalLink size={12} />
        </a>
      </div>

      {/* Human intervention banner */}
      {status === "WAITING_FOR_HUMAN" && (
        <div className="card border-amber-200 bg-amber-50 p-5">
          <div className="flex items-start gap-3">
            <ShieldAlert size={20} className="mt-0.5 shrink-0 text-amber-600" />
            <div className="flex-1">
              <p className="font-semibold text-amber-900">
                {isWaitingLive ? "Human verification required" : "Human verification timed out"}
              </p>
              <p className="mt-1 text-sm text-amber-800">
                {isWaitingLive
                  ? `A human verification challenge (CAPTCHA or similar) was detected. Please complete it in the automation's browser window — automation resumes automatically once it's cleared. ${live?.live?.reason || ""}`
                  : "The wait for human verification timed out and this run stopped. Complete the verification and retry, or start again once the page has settled."}
              </p>
              {isWaitingTimedOut && (
                <button className="btn-primary mt-3 !px-3 !py-1.5 text-xs" disabled={busy} onClick={retry}>
                  <RotateCcw size={13} /> Retry Application
                </button>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Live automation view */}
      {isActive && (
        <div className="card p-6">
          <h2 className="mb-3 flex items-center gap-2 font-semibold text-slate-900">
            <Loader2 size={16} className="animate-spin text-brand-600" /> Live Automation
          </h2>
          <div className="progress-track"><div className="progress-indeterminate" /></div>
          <div className="mt-3 grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <div>
              <p className="eyebrow">Current page</p>
              <p className="font-medium text-slate-800">{live?.live?.page_number ?? application.pages_completed ?? "—"}</p>
            </div>
            <div>
              <p className="eyebrow">Current step</p>
              <p className="font-medium text-slate-800">{(live?.live?.last_step || "starting").replaceAll("_", " ")}</p>
            </div>
            <div>
              <p className="eyebrow">Confidence so far</p>
              <p className="font-medium text-slate-800">
                {application.confidence_score != null ? `${Math.round(application.confidence_score * 100)}%` : "—"}
              </p>
            </div>
            <div>
              <p className="eyebrow">Autopilot</p>
              <p className="font-medium text-slate-800">{application.autopilot_enabled ? "Enabled" : "Copilot (you approve)"}</p>
            </div>
          </div>
        </div>
      )}

      {/* Pre-submission review gate */}
      {status === "READY_TO_SUBMIT" && summary && (
        <div className="card border-sky-200 p-6">
          <h2 className="mb-3 font-semibold text-slate-900">Review Before Submission</h2>
          <div className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            <SummaryStat label="Pages completed" value={application.pages_completed ?? "—"} />
            <SummaryStat label="Questions answered" value={`${summary.questions_answered}/${summary.questions_total}`} />
            <SummaryStat label="AI-generated answers" value={summary.questions_generated} />
            <SummaryStat label="Human-reviewed" value={summary.questions_human_reviewed} />
          </div>
          {summary.missing_fields.length > 0 && (
            <Warning title="Missing fields">{summary.missing_fields.join(", ")}</Warning>
          )}
          {summary.risky_answers.length > 0 && (
            <Warning title="Uncertain answers — review below before approving">{summary.risky_answers.join(", ")}</Warning>
          )}
          <div className="mt-4 flex gap-2">
            <button className="btn-primary" disabled={busy} onClick={approve}>
              <CheckCircle2 size={15} /> Approve & Submit
            </button>
            <button className="btn-ghost" disabled={busy} onClick={reject}>Go Back & Edit</button>
          </div>
        </div>
      )}

      {status === "SUBMITTED" && (
        <div className="card flex items-center gap-3 border-emerald-200 bg-emerald-50 p-5">
          <CheckCircle2 size={20} className="text-emerald-600" />
          <p className="text-sm text-emerald-800">
            Submitted{application.applied_date ? ` on ${new Date(application.applied_date).toLocaleString()}` : ""}.
          </p>
        </div>
      )}

      {status === "FAILED" && application.failure_reason && (
        <Warning title="Run failed">{application.failure_reason}</Warning>
      )}

      {/* Answer review — needs-your-attention first */}
      {lowConfidenceQuestions.length > 0 && (
        <div className="card p-6">
          <h2 className="mb-1 flex items-center gap-2 font-semibold text-slate-900">
            <AlertTriangle size={16} className="text-amber-600" /> Needs Your Review
          </h2>
          <p className="mb-3 text-xs text-slate-500">
            Low-confidence answers automation would not submit on your behalf without a look.
          </p>
          <AnswerReviewList questions={questions} onReview={reviewQuestion} onlyLowConfidence />
        </div>
      )}

      {/* Full question ledger */}
      <div className="card p-6">
        <h2 className="mb-3 font-semibold text-slate-900">All Questions ({questions.length})</h2>
        <AnswerReviewList questions={questions} onReview={reviewQuestion} />
      </div>

      {/* Run history / activity log */}
      <div className="card overflow-hidden">
        <div className="card-header">
          <h2 className="font-semibold text-slate-900">Activity Log</h2>
        </div>
        {runs.length === 0 ? (
          <p className="p-6 text-sm text-slate-500">No runs recorded yet.</p>
        ) : (
          <div className="divide-y divide-slate-100">
            {runs.map((r) => (
              <div key={r.run_id} className="px-6 py-3">
                <button className="flex w-full items-center justify-between text-left text-sm"
                  onClick={() => setExpandedRun(expandedRun === r.run_id ? null : r.run_id)}>
                  <span className="flex items-center gap-2 text-slate-700">
                    <FileText size={14} className="text-slate-400" />
                    {r.started_at ? new Date(r.started_at).toLocaleString() : "—"} — <StatusBadgeInline status={r.status} />
                  </span>
                  <ChevronDown size={14} className={`text-slate-400 transition-transform ${expandedRun === r.run_id ? "rotate-180" : ""}`} />
                </button>
                {expandedRun === r.run_id && (
                  <div className="mt-2 space-y-1 rounded-lg bg-slate-50 p-3 font-mono text-[11px] text-slate-600">
                    {(r.log_lines || []).length === 0 && <p>No structured log captured for this run.</p>}
                    {(r.log_lines || []).map((line, i) => (
                      <p key={i}>
                        <span className="text-slate-400">{new Date(line.timestamp).toLocaleTimeString()}</span>{" "}
                        {line.message}
                      </p>
                    ))}
                    {r.error_log && <p className="text-red-600">{r.error_log}</p>}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SummaryStat({ label, value }) {
  return (
    <div>
      <p className="eyebrow">{label}</p>
      <p className="text-lg font-semibold text-slate-900">{value}</p>
    </div>
  );
}

function Warning({ title, children }) {
  return (
    <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700">
      <p className="font-semibold">{title}</p>
      <p className="mt-0.5 text-red-700/80">{children}</p>
    </div>
  );
}

function StatusBadgeInline({ status }) {
  return <span className="font-semibold text-slate-500">{status}</span>;
}
