const STYLES = {
  READY: "badge-neutral",
  IN_PROGRESS: "badge-brand",
  WAITING_FOR_HUMAN: "badge-amber",
  WAITING_FOR_REVIEW: "badge-amber",
  READY_TO_SUBMIT: "badge-blue",
  SUBMITTED: "badge-green",
  FAILED: "badge-red",
  CANCELLED: "badge-neutral",
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
  const style = STYLES[status] || "badge-neutral";
  return <span className={`badge ${style}`}>{LABELS[status] || status}</span>;
}
