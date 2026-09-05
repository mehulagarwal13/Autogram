import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDown, CheckCircle2, FileText, Link2, ShieldCheck, Sparkles, UserRound } from "lucide-react";
import { api } from "../api";

export default function ApplicationJourney() {
  const [workflow, setWorkflow] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    api.getWorkflow().then((data) => { if (active) setWorkflow(data); })
      .catch(() => { if (active) setError("Setup status is unavailable. You can still open your profile and resumes."); });
    return () => { active = false; };
  }, []);

  const steps = [
    { title: "ONE RESUME", description: workflow?.resume_name || "Upload your resume once and reuse it for applications.", icon: FileText, to: "/resumes", done: workflow?.resume_ready },
    { title: "ONE MASTER PROFILE", description: "Review your details, experience, and answers in one place.", icon: UserRound, to: "/profile", done: workflow?.profile_ready },
    { title: "ANY JOB LINK", description: "Bring a job posting. Start with the link above.", icon: Link2, href: "#apply-from-link" },
    { title: "AUTOGRAM HELPS YOU APPLY", description: "Get help filling applications from your saved profile. Some sites need your input.", icon: Sparkles, to: "/applications" },
    { title: "YOU STAY IN CONTROL", description: "Review answers, handle verification, and choose your approval settings.", icon: ShieldCheck, to: "/settings" },
  ];
  return (
    <section className="card overflow-hidden" aria-label="How Autogram works">
      <div className="px-6 pt-5">
        <p className="text-xs font-semibold uppercase tracking-widest text-brand-600">Your application journey</p>
        <h2 className="mt-2 text-lg font-semibold tracking-tight text-slate-900">One setup. Every opportunity.</h2>
        <p className="mt-2 text-sm text-slate-500" role="status">
          {error || (workflow ? (workflow.ready_to_apply ? "Your profile and resume are ready to use." : "Add your profile and application resume to get started.") : "Checking your saved profile and resume…")}
        </p>
      </div>
      <ol className="journey-grid px-4 py-5">
        {steps.map(({ title, description, icon: Icon, to, href, done }, index) => {
          const content = <><Icon size={21} className="shrink-0 text-brand-600" /><div className="flex-1"><h3 className="text-sm font-bold tracking-wide text-slate-900">{title}</h3><p className="mt-1 text-xs leading-relaxed text-slate-500">{description}</p></div>{done && <CheckCircle2 size={19} className="shrink-0 text-emerald-600" aria-label="Ready" />}</>;
          const className = "journey-step";
          return <li key={title} className="min-w-0">{index > 0 && <ArrowDown size={18} className="mx-auto my-2 text-slate-300 md:hidden" aria-hidden="true" />}{to ? <Link to={to} className={className}>{content}</Link> : <a href={href} className={className}>{content}</a>}</li>;
        })}
      </ol>
    </section>
  );
}
