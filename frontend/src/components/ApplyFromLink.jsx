import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ExternalLink, Link2, Loader2, Rocket, ShieldAlert, Zap } from "lucide-react";
import { api } from "../api";

export default function ApplyFromLink({ toast }) {
  const [jobUrl, setJobUrl] = useState("");
  const [busy, setBusy] = useState(false);
  // Set when the backend refuses because this job already has an active run or
  // a previous successful submission. Held in state (rather than toasted) so
  // the user can act on it — including deliberately applying again.
  const [conflict, setConflict] = useState(null);
  const navigate = useNavigate();

  /**
   * `acknowledgement` is passed ONLY by the explicit "Apply again anyway"
   * action — never on a normal start, and never kept in state afterwards.
   * Retaining it would turn a deliberate one-time override into a sticky
   * setting that a later ordinary start could silently reuse.
   */
  async function startApplication(acknowledgement = null) {
    const url = jobUrl.trim();
    if (!url) return toast("Paste a job URL first.", "error");
    try {
      // eslint-disable-next-line no-new
      new URL(url);
    } catch {
      return toast("That doesn't look like a valid URL.", "error");
    }

    setBusy(true);
    if (!acknowledgement) setConflict(null);
    try {
      const application = await api.startApplication({
        job_url: url,
        ...(acknowledgement ? { acknowledge_previous_submission: acknowledgement } : {}),
      });
      toast(
        acknowledgement
          ? "Applying again — opening the job page now."
          : "Application started — opening the job page now.",
        "success",
      );
      setJobUrl("");
      setConflict(null);
      navigate(`/applications/${application.application_id}`);
    } catch (e) {
      // A previous submission, or an active run, is a conflict the user can
      // act on — shown inline rather than as a disappearing toast. A stale
      // acknowledgement asks them to reload so they see the current conflict.
      if (e.detail?.reason === "application_already_submitted"
          || e.detail?.reason === "active_automation_exists"
          || e.detail?.reason === "invalid_reapplication_request") {
        setConflict(e.detail);
      } else {
        toast(e.message, "error");
      }
    } finally {
      setBusy(false);
    }
  }

  /** The deliberate override: echoes back the exact submission being
   *  overridden, which is what the server validates. */
  function applyAgain() {
    if (busy) return;                          // double-click guard
    startApplication({
      path: conflict.path,
      task_id: conflict.task_id ?? null,
      application_id: conflict.application_id ?? null,
    });
  }

  return (
    <div className={`relative overflow-hidden rounded-2xl border border-white/10 bg-[#11182a] p-6 shadow-[0_20px_55px_-24px_rgba(15,23,42,0.55)] sm:p-7 ${busy ? "card-active" : ""}`}>
      <div className="pointer-events-none absolute -right-24 -top-28 h-72 w-72 rounded-full bg-brand-500/20 blur-3xl" />
      <div className="pointer-events-none absolute inset-0 opacity-[0.06]" style={{ backgroundImage: "radial-gradient(circle at 1px 1px, white 1px, transparent 0)", backgroundSize: "24px 24px" }} />
      <div className="relative flex flex-col gap-5 sm:flex-row sm:items-center">
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl bg-brand-gradient shadow-lg shadow-brand-950/30 ring-1 ring-white/20">
          <Rocket size={22} className="text-white" />
        </div>
        <div className="flex-1">
          <span className="mb-1.5 inline-flex items-center gap-1.5 rounded-full bg-cyan-400/10 px-2.5 py-1 text-[10px] font-bold uppercase tracking-[0.12em] text-cyan-300 ring-1 ring-cyan-300/10">
            <Zap size={11} /> Agent launchpad
          </span>
          <h2 className="text-xl font-semibold tracking-tight text-white">Turn any job link into an application</h2>
          <p className="mt-1 max-w-2xl text-sm leading-relaxed text-slate-400">
            Paste any job posting URL — the automation opens it, detects the ATS platform, fills the
            application from your profile, and hands it back to you for a final review before submitting.
          </p>
        </div>
      </div>

      <div className="relative mt-5 flex flex-col gap-2 rounded-2xl bg-white p-1.5 shadow-xl shadow-black/20 sm:flex-row">
        <input
          className="min-w-0 flex-1 rounded-xl border-0 bg-transparent px-3.5 py-3 text-sm text-slate-900 outline-none placeholder:text-slate-400 focus:ring-0"
          type="url"
          placeholder="https://company.com/careers/software-engineer"
          value={jobUrl}
          onChange={(e) => setJobUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && startApplication()}
          disabled={busy}
        />
        {/* `() => startApplication()` deliberately, NOT `onClick={startApplication}`:
            React would pass the click event as the first argument, which
            `startApplication` would treat as an acknowledgement — a truthy
            MouseEvent silently sent as `acknowledge_previous_submission`. The
            server rejects it, but a normal start must never even look like an
            override attempt. */}
        <button className="btn-primary shrink-0 !rounded-xl !px-5 !py-3 sm:w-auto" onClick={() => startApplication()} disabled={busy}>
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Link2 size={16} />}
          {busy ? "Starting..." : "Start Application"}
        </button>
      </div>

      {conflict && (
        <div className="relative mt-4 rounded-xl border border-amber-200 bg-amber-50 p-3.5">
          <div className="flex items-start gap-2.5">
            <ShieldAlert size={16} className="mt-0.5 shrink-0 text-amber-600" />
            <div className="flex-1">
              <p className="text-sm font-medium text-slate-900">
                {conflict.reason === "application_already_submitted"
                  ? "You've already applied to this job"
                  : conflict.reason === "invalid_reapplication_request"
                    ? "This page is out of date"
                    : "This job is already being automated"}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-slate-600">{conflict.message}</p>
              {conflict.submitted_at && (
                <p className="mt-1 text-xs text-slate-500">
                  Submitted {new Date(conflict.submitted_at).toLocaleString()}
                </p>
              )}
              {conflict.application_id && (
                <Link
                  to={`/applications/${conflict.application_id}`}
                  className="mt-2 inline-flex items-center gap-1.5 text-xs font-medium text-brand-600 hover:underline"
                >
                  <ExternalLink size={13} /> View that application
                </Link>
              )}
              {conflict.reason === "invalid_reapplication_request" && (
                <p className="mt-2 text-[11px] text-slate-500">Reload the page and try again.</p>
              )}

              {/* The deliberate re-application. Offered only for a genuine
                  prior submission — never for an active run, which must finish
                  or be cancelled first — and never as the default: the warning
                  comes first and Cancel is the primary choice. */}
              {conflict.reason === "application_already_submitted" && (
                <div className="mt-3 border-t border-amber-200 pt-3">
                  <p className="text-xs font-medium text-slate-800">
                    Applying again may create a duplicate application with the employer.
                  </p>
                  <p className="mt-1 text-[11px] leading-relaxed text-slate-600">
                    Your previous application is kept either way — this starts a new, separate attempt.
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
    </div>
  );
}
