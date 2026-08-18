import { useState } from "react";
import { Briefcase, Loader2, CheckCircle2 } from "lucide-react";
import { api } from "../api";

const COUNTRIES = [
  { code: "in", label: "India" },
  { code: "us", label: "USA" },
  { code: "gb", label: "UK" },
  { code: "de", label: "Germany" },
  { code: "ca", label: "Canada" },
  { code: "au", label: "Australia" },
];

export default function JobsPanel({ toast }) {
  const [query, setQuery] = useState("");
  const [country, setCountry] = useState("in");
  const [location, setLocation] = useState("");
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");
  const [result, setResult] = useState(null);

  async function fetchAndIndex() {
    if (!query.trim()) return toast("Enter a job search query first.", "error");
    setBusy(true);
    setResult(null);
    try {
      setPhase("Scraping live listings from Adzuna...");
      const ingest = await api.ingestJobs({ query, sources: "adzuna", country, location, results: 30 });
      setPhase("Indexing jobs for AI matching...");
      const embed = await api.embedPending();
      setResult({ ingested: ingest.total_ingested, indexed: embed.embedded_count });
      toast(`Fetched ${ingest.total_ingested} jobs, indexed ${embed.embedded_count} — ready to match.`, "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
      setPhase("");
    }
  }

  return (
    <div className={`card animate-fade-up p-6 ${busy ? "card-active" : ""}`}>
      <h2 className="text-base font-semibold text-slate-900">Job Sourcing</h2>
      <p className="mt-0.5 mb-5 text-sm text-slate-500">
        Live listings from Adzuna, cleaned and indexed for matching.
      </p>

      <div className="grid gap-3 sm:grid-cols-2">
        <input className="input sm:col-span-2" placeholder="Search query — e.g. backend engineer"
          value={query} onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && fetchAndIndex()} />
        <select className="input" value={country} onChange={(e) => setCountry(e.target.value)}>
          {COUNTRIES.map((c) => <option key={c.code} value={c.code}>{c.label}</option>)}
        </select>
        <input className="input" placeholder="City filter (optional)"
          value={location} onChange={(e) => setLocation(e.target.value)} />
      </div>

      <button className="btn-primary mt-4 w-full" onClick={fetchAndIndex} disabled={busy}>
        {busy ? <Loader2 size={16} className="animate-spin" /> : <Briefcase size={16} />}
        {busy ? "Working..." : "Fetch & Index Jobs"}
      </button>

      {busy && (
        <div className="mt-4 animate-fade-up">
          <div className="progress-track"><div className="progress-indeterminate" /></div>
          <p className="mt-2 text-center text-xs font-medium text-brand-600">{phase}</p>
        </div>
      )}

      {result && (
        <div className="mt-4 flex animate-fade-up items-center gap-5 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm">
          <CheckCircle2 size={16} className="text-emerald-600" />
          <span className="text-emerald-800"><b>{result.ingested}</b> fetched</span>
          <span className="text-emerald-800"><b>{result.indexed}</b> indexed</span>
        </div>
      )}
    </div>
  );
}
