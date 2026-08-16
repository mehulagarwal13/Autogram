import { useState } from "react";
import { Check, Pencil, X, ShieldQuestion } from "lucide-react";

const CONFIDENCE_STYLE = {
  HIGH: "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/25",
  MEDIUM: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/25",
  LOW: "bg-rose-500/15 text-rose-300 ring-1 ring-rose-400/25",
};

const SOURCE_LABEL = {
  profile: "From your profile",
  answer_memory: "Reused from a past answer",
  llm: "AI-generated",
  vision: "Read from the page",
  needs_user_input: "Needs your input",
  human: "You provided this",
};

const REVIEWED_STATUSES = new Set(["approved", "edited", "rejected"]);

function QuestionRow({ question, onReview }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(question.human_answer || question.answer || "");
  const [busy, setBusy] = useState(false);
  const reviewed = REVIEWED_STATUSES.has(question.review_status);
  const displayAnswer = question.human_answer || question.answer;

  async function act(action, answer) {
    setBusy(true);
    try {
      await onReview(question.question_id, action, answer);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`rounded-xl border p-4 ${
      question.confidence_level === "LOW" && !reviewed
        ? "border-rose-400/30 bg-rose-500/[0.04]"
        : "border-white/[0.06] bg-white/[0.02]"
    }`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <p className="max-w-xl text-sm font-medium text-slate-200">{question.question_text}</p>
        <div className="flex shrink-0 items-center gap-1.5">
          <span className={`chip ${CONFIDENCE_STYLE[question.confidence_level] || ""}`}>
            {question.confidence_level}
          </span>
          {reviewed && <span className="chip bg-white/[0.06] text-slate-400">{question.review_status}</span>}
        </div>
      </div>

      <p className="mt-1 text-[11px] text-slate-500">
        {SOURCE_LABEL[question.source] || question.source}
        {question.page_number ? ` · page ${question.page_number}` : ""}
      </p>

      {editing ? (
        <div className="mt-2 space-y-2">
          {question.available_options?.length ? (
            <select className="input-dark" value={draft} onChange={(e) => setDraft(e.target.value)}>
              <option value="">Choose an option...</option>
              {question.available_options.map((o) => <option key={o} value={o}>{o}</option>)}
            </select>
          ) : (
            <textarea className="input-dark" rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} />
          )}
          <div className="flex gap-2">
            <button className="btn-primary !px-3 !py-1.5 text-xs" disabled={busy || !draft.trim()}
              onClick={() => act("edit", draft)}>
              <Check size={13} /> Save answer
            </button>
            <button className="btn-ghost !px-3 !py-1.5 text-xs" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
          <p className={`text-sm ${displayAnswer ? "text-slate-300" : "italic text-slate-600"}`}>
            {displayAnswer || "No answer yet — needs your input."}
          </p>
          {!reviewed && (
            <div className="flex shrink-0 gap-1.5">
              {displayAnswer && (
                <button className="btn-ghost !px-2.5 !py-1 text-xs !text-emerald-300 hover:!border-emerald-400/40"
                  disabled={busy} onClick={() => act("approve")}>
                  <Check size={13} /> Approve
                </button>
              )}
              <button className="btn-ghost !px-2.5 !py-1 text-xs" disabled={busy}
                onClick={() => { setDraft(question.answer || ""); setEditing(true); }}>
                <Pencil size={13} /> Edit
              </button>
              <button className="btn-ghost !px-2.5 !py-1 text-xs !text-rose-300/80 hover:!border-rose-400/40"
                disabled={busy} onClick={() => act("reject")}>
                <X size={13} /> Reject
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function AnswerReviewList({ questions, onReview, onlyLowConfidence = false }) {
  const filtered = onlyLowConfidence
    ? questions.filter((q) => q.confidence_level === "LOW" && !REVIEWED_STATUSES.has(q.review_status))
    : questions;

  if (filtered.length === 0) {
    return (
      <div className="flex flex-col items-center px-6 py-10 text-center">
        <ShieldQuestion size={28} className="mb-2 text-slate-600" />
        <p className="text-sm text-slate-500">
          {onlyLowConfidence ? "Nothing needs your review right now." : "No questions recorded yet for this application."}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {filtered.map((q) => <QuestionRow key={q.question_id} question={q} onReview={onReview} />)}
    </div>
  );
}
