import { Check, Lock } from "lucide-react";
import UploadPanel from "../components/UploadPanel";
import JobsPanel from "../components/JobsPanel";
import MatchesPanel from "../components/MatchesPanel";

// A numbered step wrapper — not a form flow of its own (each inner panel
// still owns its state), just a consistent "what step is this, is it done,
// is it locked" frame so the three-panel layout reads as an ordered
// sequence instead of three unrelated cards.
function StepPanel({ step, title, done, locked, lockedHint, children }) {
  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
          done ? "bg-emerald-100 text-emerald-700" : "bg-slate-100 text-slate-500"
        }`}>
          {done ? <Check size={13} /> : step}
        </span>
        <p className="text-sm font-semibold text-slate-700">{title}</p>
      </div>
      <div className="relative">
        <div className={locked ? "pointer-events-none select-none opacity-40" : ""}>{children}</div>
        {locked && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 rounded-xl bg-white/70 p-6 text-center">
            <Lock size={18} className="text-slate-400" />
            <p className="max-w-xs text-xs font-medium text-slate-600">{lockedHint}</p>
          </div>
        )}
      </div>
    </div>
  );
}

export default function JobsAndMatches({ resume, setResume, toast }) {
  const hasResume = Boolean(resume);

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="page-title">Job Search</h1>
        <p className="page-subtitle">
          Three steps, in order: upload a resume, source live listings, then review AI-ranked matches.
        </p>
      </div>

      <div className="grid items-start gap-6 xl:grid-cols-[340px_minmax(0,1fr)]">
        <div className="space-y-6">
          <StepPanel step={1} title="Upload your resume" done={hasResume}>
            <UploadPanel resume={resume} setResume={setResume} toast={toast} />
          </StepPanel>

          <StepPanel
            step={2}
            title="Find jobs"
            locked={!hasResume}
            lockedHint="Upload your resume above to unlock job search."
          >
            <JobsPanel toast={toast} />
          </StepPanel>
        </div>

        <StepPanel
          step={3}
          title="Review matches"
          locked={!hasResume}
          lockedHint="Matches appear here once you've uploaded a resume and sourced some jobs."
        >
          <MatchesPanel resume={resume} toast={toast} />
        </StepPanel>
      </div>
    </div>
  );
}
