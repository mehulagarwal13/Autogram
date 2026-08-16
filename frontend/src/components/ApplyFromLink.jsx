import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link2, Loader2, Rocket } from "lucide-react";
import { api } from "../api";

export default function ApplyFromLink({ toast }) {
  const [jobUrl, setJobUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();

  async function startApplication() {
    const url = jobUrl.trim();
    if (!url) return toast("Paste a job URL first.", "error");
    try {
      // eslint-disable-next-line no-new
      new URL(url);
    } catch {
      return toast("That doesn't look like a valid URL.", "error");
    }

    setBusy(true);
    try {
      const application = await api.startApplication({ job_url: url });
      toast("Application started — opening the job page now.", "success");
      setJobUrl("");
      navigate(`/applications/${application.application_id}`);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`glass animate-fade-up p-6 ${busy ? "glass-active" : ""}`}>
      <h2 className="mb-1 flex items-center gap-2 text-lg font-bold text-white">
        <Link2 size={17} className="text-indigo-300" /> Apply from Job Link
      </h2>
      <p className="mb-4 text-sm text-slate-400">
        Paste any job posting URL — the automation opens it, finds the Apply control if there is one,
        detects the ATS platform, and runs the same review-and-approve workflow as every other application.
      </p>

      <div className="flex flex-col gap-3 sm:flex-row">
        <input
          className="input-dark flex-1"
          type="url"
          placeholder="https://company.com/careers/software-engineer"
          value={jobUrl}
          onChange={(e) => setJobUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && startApplication()}
          disabled={busy}
        />
        <button className="btn-primary shrink-0 justify-center sm:w-auto" onClick={startApplication} disabled={busy}>
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Rocket size={16} />}
          {busy ? "Starting..." : "Start Application"}
        </button>
      </div>
    </div>
  );
}
