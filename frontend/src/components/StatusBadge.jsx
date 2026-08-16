const STYLES = {
  READY: "bg-slate-500/15 text-slate-300 ring-1 ring-slate-400/25",
  IN_PROGRESS: "bg-indigo-500/15 text-indigo-300 ring-1 ring-indigo-400/25",
  WAITING_FOR_HUMAN: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/25",
  WAITING_FOR_REVIEW: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/25",
  READY_TO_SUBMIT: "bg-sky-500/15 text-sky-300 ring-1 ring-sky-400/25",
  SUBMITTED: "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/25",
  FAILED: "bg-rose-500/15 text-rose-300 ring-1 ring-rose-400/25",
  CANCELLED: "bg-slate-500/10 text-slate-500 ring-1 ring-slate-500/20",
};

const LABELS = {
  READY: "Ready",
  IN_PROGRESS: "In Progress",
  WAITING_FOR_HUMAN: "Waiting for You",
  WAITING_FOR_REVIEW: "Needs Review",
  READY_TO_SUBMIT: "Ready to Submit",
  SUBMITTED: "Submitted",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
};

export default function StatusBadge({ status }) {
  const style = STYLES[status] || "bg-white/[0.06] text-slate-400 ring-1 ring-white/10";
  return <span className={`chip ${style}`}>{LABELS[status] || status}</span>;
}
