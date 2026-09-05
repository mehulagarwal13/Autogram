import { useEffect, useState } from "react";
import { Loader2, Gauge } from "lucide-react";
import { api } from "../api";

function formatHours(hours) {
  if (hours == null) return "—";
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours < 48) return `${hours.toFixed(1)} hr`;
  return `${(hours / 24).toFixed(1)} days`;
}

function formatRate(rate) {
  return rate == null ? "—" : `${Math.round(rate * 100)}%`;
}

function Stat({ label, value, hint }) {
  return (
    <div>
      <p className="eyebrow">{label}</p>
      <p className="text-2xl font-semibold text-slate-900">{value}</p>
      {hint && <p className="mt-0.5 text-xs text-slate-400">{hint}</p>}
    </div>
  );
}

function EngineCard({ title, description, total, children }) {
  return (
    <div className="card p-6">
      <h2 className="font-semibold text-slate-900">{title}</h2>
      <p className="mt-1 text-xs text-slate-500">{description}</p>
      {total === 0 ? (
        <p className="mt-6 text-sm text-slate-400">No runs yet — nothing to measure.</p>
      ) : (
        <div className="mt-5 grid grid-cols-2 gap-5 sm:grid-cols-4">{children}</div>
      )}
    </div>
  );
}

export default function Metrics({ toast }) {
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getMetricsSummary()
      .then(setSummary)
      .catch((e) => toast(e.message, "error"))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;
  }
  if (!summary) return <div className="empty-state"><h1 className="page-title">Metrics unavailable</h1><p className="page-subtitle">We couldn’t load your activity. Please try again.</p><button className="btn-ghost mt-5" onClick={() => window.location.reload()}>Try again</button></div>;

  const { deterministic: d, autonomous: a } = summary;

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <p className="page-kicker"><Gauge size={14} /> A little perspective</p>
        <h1 className="page-title">Your progress</h1>
        <p className="page-subtitle">
          How well the automation is actually working, computed from real run history — not a target, a measurement.
        </p>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <EngineCard
          title="Applications" total={d.total}
          description={`${d.total} attempt${d.total === 1 ? "" : "s"} through the per-site adapter pipeline (Greenhouse, Lever, Workday, and a generic fallback).`}
        >
          <Stat label="Total attempts" value={d.total} />
          <Stat label="Median time to outcome" value={formatHours(d.median_hours_to_outcome)} hint="paste to finished, wall-clock" />
          <Stat label="Clean submission rate" value={formatRate(d.clean_submission_rate)} hint="applied with no review needed" />
          <Stat label="Auto-answered questions" value={formatRate(d.auto_answered_question_rate)} hint="never asked you" />
        </EngineCard>

        <EngineCard
          title="Autonomous Agent" total={a.total}
          description={`${a.total} task${a.total === 1 ? "" : "s"} through the general-purpose observe-decide-act agent.`}
        >
          <Stat label="Total tasks" value={a.total} />
          <Stat label="Median time to outcome" value={formatHours(a.median_hours_to_outcome)} hint="paste to finished, wall-clock" />
          <Stat label="HITL resolution rate" value={formatRate(a.hitl_resolution_rate)} hint="you resolved it, didn't time out" />
          <Stat label="Fully autonomous rate" value={formatRate(a.fully_autonomous_completion_rate)} hint="completed with zero pauses" />
        </EngineCard>
      </div>

      <p className="max-w-2xl text-xs text-slate-400">
        "Clean submission" and "auto-answered" are the closest honest measurements available today, not a literal
        count of field errors — Autogram doesn't yet have a way for you to flag a mistake after the fact. Both are
        computed only from applications/tasks that have already reached a final outcome.
      </p>
    </div>
  );
}
