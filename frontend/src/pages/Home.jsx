import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, ArrowUpRight, CheckCircle2, Clock3, FileText, LayoutDashboard, Loader2, Search, ShieldCheck, Sparkles } from "lucide-react";
import { api } from "../api";
import ApplyFromLink from "../components/ApplyFromLink";
import ApplicationJourney from "../components/ApplicationJourney";
import StatusBadge from "../components/StatusBadge";
import { SkeletonTile } from "../components/Skeleton";

const STATS = [
  { key: "total", label: "Applications", icon: LayoutDashboard },
  { key: "submitted", label: "Submitted", icon: CheckCircle2 },
  { key: "in_progress", label: "In progress", icon: Loader2 },
  { key: "waiting_for_human", label: "Needs your attention", icon: Clock3 },
];

export default function Home({ user, toast }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(false);
  async function load() {
    setError(false);
    try {
      const [overview, applications] = await Promise.all([api.getApplicationsOverview(), api.listApplications()]);
      setData({ overview, applications });
    } catch (error) { setError(true); toast(error.message, "error"); }
  }
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const name = user?.email?.split("@")[0]?.split(/[._-]/)[0] || "there";
  return <div className="animate-fade-up space-y-7">
    <div className="page-heading">
      <div><p className="page-kicker"><Sparkles size={14} /> Your next chapter</p><h1 className="page-title">Welcome back, <span className="capitalize">{name}.</span></h1><p className="page-subtitle">Less time on forms. More room for your next opportunity.</p></div>
      <Link to="/profile" className="btn-ghost"><FileText size={15} /> My master profile <ArrowUpRight size={14} /></Link>
    </div>
    <div id="apply-from-link" className="scroll-mt-24"><ApplyFromLink toast={toast} /></div>
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {STATS.map(({ key, label, icon: Icon }) => !data && !error ? <SkeletonTile key={key} /> : <Link key={key} to="/applications" className="card card-hover p-5"><div className="flex items-center justify-between"><Icon size={18} className="text-brand-600" /><ArrowUpRight size={14} className="text-slate-300" /></div><p className="mt-5 text-3xl font-semibold tracking-tight text-slate-900">{data?.overview?.[key] ?? "—"}</p><p className="mt-1.5 text-xs font-medium text-slate-500">{label}</p></Link>)}
    </div>
    <ApplicationJourney />
    <div className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_300px]">
      <section className="card overflow-hidden"><div className="card-header"><div><h2 className="font-semibold text-slate-900">Recent applications</h2><p className="mt-1 text-xs text-slate-500">Pick up where you left off.</p></div><Link to="/applications" className="text-xs font-semibold text-brand-600">View all <ArrowRight size={13} className="inline" /></Link></div>
        {error ? <div className="empty-state"><p className="mb-4 text-sm text-slate-500">We couldn’t load your applications.</p><button onClick={load} className="btn-ghost">Try again</button></div> : !data ? <div className="p-10 text-center text-sm text-slate-500">Loading your activity…</div> : data.applications.length === 0 ? <div className="empty-state"><span className="empty-state-icon"><LayoutDashboard size={24} /></span><h3 className="font-semibold">Your next opportunity starts here</h3><p className="mt-2 max-w-xs text-sm leading-relaxed text-slate-500">Add a job link above. We’ll help you take it from a posting to an application.</p><a href="#apply-from-link" className="btn-primary mt-5">Add a job link <ArrowRight size={14} /></a></div> : <div className="divide-y divide-slate-100">{data.applications.slice(0, 5).map((application) => <Link key={application.application_id} to={`/applications/${application.application_id}`} className="flex flex-wrap items-center justify-between gap-3 px-5 py-4 hover:bg-slate-50"><div className="flex min-w-0 items-center gap-3"><span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-50 font-semibold text-brand-700">{(application.company || "?")[0]}</span><div className="min-w-0"><p className="truncate text-sm font-semibold">{application.position || "Untitled role"}</p><p className="mt-1 text-xs text-slate-500">{application.company || "Unknown company"}</p></div></div><StatusBadge status={application.display_status} /></Link>)}</div>}
      </section>
      <aside className="space-y-4"><div className="rounded-2xl border border-brand-200 bg-brand-50 p-6"><ShieldCheck size={24} className="text-brand-700" /><h2 className="mt-4 font-semibold text-brand-900">Your career. Your call.</h2><p className="mt-2 text-sm leading-relaxed text-brand-800">Choose how much help you want and when Autogram should pause for you.</p><Link to="/settings" className="mt-5 inline-flex items-center gap-2 text-xs font-semibold text-brand-700">Review your controls <ArrowRight size={14} /></Link></div><Link to="/search" className="card card-hover flex items-center gap-3 p-5"><Search size={20} className="text-brand-600" /><div className="flex-1"><p className="text-sm font-semibold">Find your next role</p><p className="mt-1 text-xs text-slate-500">Explore jobs matched to you</p></div><ArrowUpRight size={16} className="text-slate-400" /></Link></aside>
    </div>
  </div>;
}
