import { PieChart } from "lucide-react";

// Part-to-whole composition of application statuses, drawn as a single
// horizontal stacked bar. Colors are the same reserved semantic status
// colors used by StatusBadge across the app (never repurposed as generic
// series colors), so the mapping stays consistent everywhere it appears.
const SEGMENT_DEFS = [
  { key: "submitted", label: "Submitted", bar: "#10b981", dot: "bg-emerald-500" },
  { key: "in_progress", label: "In Progress", bar: "#6366f1", dot: "bg-brand-500" },
  { key: "needs_attention", label: "Needs Attention", bar: "#f59e0b", dot: "bg-amber-500" },
  { key: "failed", label: "Failed", bar: "#ef4444", dot: "bg-red-500" },
  { key: "cancelled", label: "Cancelled", bar: "#cbd5e1", dot: "bg-slate-300" },
  { key: "other", label: "Other", bar: "#e2e8f0", dot: "bg-slate-200" },
];

export default function StatusBreakdown({ overview }) {
  const raw = {
    submitted: overview?.submitted || 0,
    in_progress: overview?.in_progress || 0,
    needs_attention: (overview?.waiting_for_human || 0) + (overview?.waiting_for_review || 0),
    failed: overview?.failed || 0,
    cancelled: overview?.cancelled || 0,
  };
  const knownTotal = Object.values(raw).reduce((s, v) => s + v, 0);
  raw.other = Math.max((overview?.total || 0) - knownTotal, 0);

  const segments = SEGMENT_DEFS.map((s) => ({ ...s, value: raw[s.key] || 0 })).filter((s) => s.value > 0);
  const total = segments.reduce((s, x) => s + x.value, 0);

  if (total === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-center">
        <PieChart size={26} className="mb-2 text-slate-300" />
        <p className="text-sm text-slate-500">No applications yet to break down.</p>
      </div>
    );
  }

  return (
    <div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-slate-100">
        {segments.map((s, i) => (
          <div
            key={s.key}
            title={`${s.label}: ${s.value} (${Math.round((s.value / total) * 100)}%)`}
            style={{
              width: `${(s.value / total) * 100}%`,
              backgroundColor: s.bar,
              borderRight: i < segments.length - 1 ? "2px solid white" : "none",
            }}
          />
        ))}
      </div>
      <div className="mt-4 flex flex-wrap gap-x-5 gap-y-2">
        {segments.map((s) => (
          <div key={s.key} className="flex items-center gap-1.5 text-xs">
            <span className={`h-2 w-2 rounded-full ${s.dot}`} />
            <span className="text-slate-600">{s.label}</span>
            <span className="font-semibold text-slate-900">{s.value}</span>
            <span className="text-slate-400">({Math.round((s.value / total) * 100)}%)</span>
          </div>
        ))}
      </div>
    </div>
  );
}
