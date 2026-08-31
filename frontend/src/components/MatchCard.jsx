import { useState } from "react";
import { MapPin, Building2, ExternalLink, Bookmark, XCircle, ChevronDown, Rocket } from "lucide-react";
import ScoreRing from "./ScoreRing";

function Bar({ label, value, color }) {
  return (
    <div>
      <div className="mb-1 flex justify-between text-[11px]">
        <span className="text-slate-500">{label}</span>
        <span className="font-medium text-slate-700">{Math.round(value * 100)}%</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div className={`h-full rounded-full ${color}`}
          style={{ width: `${Math.round(value * 100)}%`, transition: "width 0.6s ease" }} />
      </div>
    </div>
  );
}

export default function MatchCard({ match, onStatus, onApply, applying }) {
  const [open, setOpen] = useState(false);
  const isSaved = match.status === "saved";
  const isDismissed = match.status === "dismissed";

  return (
    <div className={`card card-hover p-5 ${isDismissed ? "opacity-50" : ""}`}>
      <div className="flex items-start gap-4">
        <ScoreRing value={match.blended_score} label="match" />

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h3 className="truncate font-semibold text-slate-900">{match.title}</h3>
              <p className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-slate-500">
                {match.company && <span className="flex items-center gap-1"><Building2 size={12} />{match.company}</span>}
                {match.location && <span className="flex items-center gap-1"><MapPin size={12} />{match.location}</span>}
              </p>
            </div>
            {isSaved && <span className="badge badge-green shrink-0">saved</span>}
            {isDismissed && <span className="badge badge-red shrink-0">dismissed</span>}
          </div>

          <div className="mt-3 grid grid-cols-3 gap-3">
            <Bar label="Semantic" value={match.vector_similarity} color="bg-brand-500" />
            <Bar label="Skills" value={match.skill_overlap_ratio} color="bg-fuchsia-500" />
            <Bar label="ATS" value={match.ats_score} color="bg-sky-500" />
          </div>
        </div>
      </div>

      {match.explanation && (
        <p className="mt-3 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-600">
          {match.explanation}
        </p>
      )}

      <button onClick={() => setOpen(!open)}
        className="mt-3 flex w-full items-center justify-center gap-1 text-xs text-slate-500 transition hover:text-slate-800">
        {match.matched_skills.length} matched · {match.missing_skills.length} missing skills
        <ChevronDown size={14} className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </button>

      {open && (
        <div className="mt-3 animate-fade-up space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {match.matched_skills.map((s) => (
              <span key={s} className="badge badge-green">{s}</span>
            ))}
            {match.missing_skills.map((s) => (
              <span key={s} className="badge badge-red">{s}</span>
            ))}
          </div>
          {match.ats_missing_keywords.length > 0 && (
            <p className="text-[11px] text-slate-500">
              ATS keywords to add: {match.ats_missing_keywords.slice(0, 8).join(", ")}
            </p>
          )}
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
        <div className="flex-1" />
        {!isSaved && (
          <button className="btn-ghost !px-3 !py-1.5 text-xs !text-emerald-700"
            onClick={() => onStatus(match, "saved")}>
            <Bookmark size={14} /> Save
          </button>
        )}
        {!isDismissed && (
          <button className="btn-ghost !px-3 !py-1.5 text-xs !text-red-700"
            onClick={() => onStatus(match, "dismissed")}>
            <XCircle size={14} /> Dismiss
          </button>
        )}
        {match.apply_url && (
          <>
            <a href={match.apply_url} target="_blank" rel="noreferrer"
              className="btn-ghost !px-3 !py-1.5 text-xs" title="View the original posting">
              <ExternalLink size={13} />
            </a>
            <button className="btn-primary !px-3 !py-1.5 text-xs" disabled={applying} onClick={() => onApply?.(match)}>
              <Rocket size={13} /> {applying ? "Starting..." : "Start Application"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}
