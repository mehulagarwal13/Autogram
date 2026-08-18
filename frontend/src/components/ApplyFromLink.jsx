import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Link2, Loader2, Rocket, Zap } from "lucide-react";
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
    <div className={`relative overflow-hidden rounded-xl border-2 border-brand-200 bg-brand-50/60 p-6 sm:p-7 ${busy ? "card-active" : ""}`}>
      <div className="flex flex-col gap-5 sm:flex-row sm:items-center">
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-brand-600 shadow-sm">
          <Rocket size={22} className="text-white" />
        </div>
        <div className="flex-1">
          <span className="badge badge-brand mb-1.5">
            <Zap size={11} /> Quick Action
          </span>
          <h2 className="text-lg font-semibold text-slate-900">Apply from a job link</h2>
          <p className="mt-1 max-w-2xl text-sm text-slate-600">
            Paste any job posting URL — the automation opens it, detects the ATS platform, fills the
            application from your profile, and hands it back to you for a final review before submitting.
          </p>
        </div>
      </div>

      <div className="mt-5 flex flex-col gap-3 sm:flex-row">
        <input
          className="input flex-1 !bg-white !py-3"
          type="url"
          placeholder="https://company.com/careers/software-engineer"
          value={jobUrl}
          onChange={(e) => setJobUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !busy && startApplication()}
          disabled={busy}
        />
        <button className="btn-primary shrink-0 !py-3 sm:w-auto" onClick={startApplication} disabled={busy}>
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Link2 size={16} />}
          {busy ? "Starting..." : "Start Application"}
        </button>
      </div>
    </div>
  );
}
