import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Target, Loader2, Sparkles } from "lucide-react";
import { api } from "../api";
import MatchCard from "./MatchCard";

const PHASES = [
  "Finding semantically similar jobs...",
  "Applying experience & location filters...",
  "AI analyzing skill fit per job...",
  "Computing ATS keyword scores...",
];

export default function MatchesPanel({ resume, toast }) {
  const [matches, setMatches] = useState([]);
  const [generating, setGenerating] = useState(false);
  const [phaseIdx, setPhaseIdx] = useState(0);
  const [filter, setFilter] = useState("");
  const [locationFilter, setLocationFilter] = useState("");
  const [applyingId, setApplyingId] = useState(null);
  const navigate = useNavigate();

  // Restore previously generated matches when a resume appears (e.g. after login)
  useEffect(() => {
    if (!resume?.id) { setMatches([]); return; }
    api.listMatches(resume.id).then((r) => setMatches(r.results)).catch(() => {});
  }, [resume?.id]);

  async function generate() {
    if (!resume) return toast("Upload a resume first.", "error");
    setGenerating(true);
    setPhaseIdx(0);
    const ticker = setInterval(() => setPhaseIdx((i) => Math.min(i + 1, PHASES.length - 1)), 4000);
    try {
      const r = await api.generateMatches(resume.id, { location: locationFilter });
      setMatches(r.results);
      toast(`Found ${r.result_count} matching jobs, ranked by fit.`, "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      clearInterval(ticker);
      setGenerating(false);
    }
  }

  async function refresh(status) {
    if (!resume) return;
    setFilter(status);
    try {
      const r = await api.listMatches(resume.id, status || undefined);
      setMatches(r.results);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function setStatus(match, status) {
    try {
      await api.setMatchStatus(match.match_id, status);
      setMatches((ms) => ms.map((m) => (m.match_id === match.match_id ? { ...m, status } : m)));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function startApplication(match) {
    if (!match.apply_url) return;
    setApplyingId(match.match_id);
    try {
      const dup = await api.checkDuplicateApplication({ company: match.company, position: match.title }).catch(() => null);
      if (dup?.possible_duplicate) {
        const proceed = window.confirm(
          `You may have already applied to a similar role at ${match.company} (status: ${dup.existing_status}). Start a new application anyway?`
        );
        if (!proceed) { setApplyingId(null); return; }
      }
      const application = await api.startApplication({
        job_url: match.apply_url, company: match.company, position: match.title,
      });
      toast(`Application started for ${match.title} at ${match.company || "this company"}.`, "success");
      navigate(`/applications/${application.application_id}`);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setApplyingId(null);
    }
  }

  return (
    <div className="animate-fade-up space-y-4">
      {/* Controls */}
      <div className={`card flex flex-wrap items-center gap-3 p-4 ${generating ? "card-active" : ""}`}>
        <input className="input !w-48 flex-1" placeholder="Location filter (optional)"
          value={locationFilter} onChange={(e) => setLocationFilter(e.target.value)} />
        <button className="btn-primary" onClick={generate} disabled={generating || !resume}>
          {generating ? <Loader2 size={16} className="animate-spin" /> : <Target size={16} />}
          {generating ? "Matching..." : "Generate Matches"}
        </button>
        <div className="ml-auto flex gap-1 rounded-lg bg-slate-100 p-1">
          {[["", "All"], ["new", "New"], ["saved", "Saved"], ["dismissed", "Dismissed"]].map(([v, l]) => (
            <button key={v} onClick={() => refresh(v)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                filter === v ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
              {l}
            </button>
          ))}
        </div>
      </div>

      {generating && (
        <div className="card p-6">
          <div className="progress-track"><div className="progress-indeterminate" /></div>
          <p className="mt-3 flex items-center justify-center gap-2 text-xs text-brand-600">
            <Sparkles size={13} />
            {PHASES[phaseIdx]}
          </p>
        </div>
      )}

      {!generating && matches.length === 0 && (
        <div className="card flex flex-col items-center p-12 text-center">
          <Target size={32} className="mb-3 text-slate-300" />
          <p className="font-medium text-slate-700">No matches yet</p>
          <p className="mt-1 max-w-sm text-xs text-slate-500">
            Upload a resume, fetch some jobs, then hit Generate Matches.
          </p>
        </div>
      )}

      {matches.map((m) => (
        <div key={m.match_id} className="animate-fade-up">
          <MatchCard match={m} onStatus={setStatus} onApply={startApplication} applying={applyingId === m.match_id} />
        </div>
      ))}
    </div>
  );
}
