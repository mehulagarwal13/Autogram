import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity, ArrowRight, ArrowUpRight, CheckCircle2, Clock3, FileText,
  FileWarning, LayoutDashboard, Loader2, Search, Settings as SettingsIcon,
  Sparkles, Target, TrendingUp,
} from "lucide-react";
import { api } from "../api";
import ApplyFromLink from "../components/ApplyFromLink";
import StatusBadge from "../components/StatusBadge";
import StatusBreakdown from "../components/StatusBreakdown";
import { SkeletonRow, SkeletonTile } from "../components/Skeleton";

const TILES = [
  { key: "total", label: "Total applications", icon: LayoutDashboard, accent: "text-slate-700", bg: "bg-slate-100" },
  { key: "submitted", label: "Submitted", icon: CheckCircle2, accent: "text-emerald-600", bg: "bg-emerald-50" },
  { key: "in_progress", label: "In progress", icon: Loader2, accent: "text-brand-600", bg: "bg-brand-50" },
  { key: "waiting_for_human", label: "Needs attention", icon: Clock3, accent: "text-amber-600", bg: "bg-amber-50" },
];

const QUICK_LINKS = [
  { to: "/search", label: "Discover roles", desc: "Source and score live jobs", icon: Search, color: "text-sky-600 bg-sky-50" },
  { to: "/resumes", label: "Resume library", desc: "Manage targeted variants", icon: FileText, color: "text-violet-600 bg-violet-50" },
  { to: "/settings", label: "Agent controls", desc: "Set safety and approval rules", icon: SettingsIcon, color: "text-amber-600 bg-amber-50" },
];

function GettingStarted({ resumeReady, hasApplied }) {
  const steps = [
    { done: resumeReady, title: "Add your resume", desc: "Give the agent the context it needs.", to: "/resumes" },
    { done: hasApplied, title: "Launch an application", desc: "Paste a job URL or choose a match.", to: "/search" },
  ];

  return (
    <div className="card p-5">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <p className="text-sm font-semibold text-slate-900">Complete your workspace</p>
          <p className="mt-0.5 text-xs text-slate-500">A quick setup before your agent gets to work.</p>
        </div>
        <span className="rounded-full bg-brand-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wide text-brand-700">
          {steps.filter((step) => step.done).length}/2 ready
        </span>
      </div>
      <div className="space-y-2">
        {steps.map((step, index) => (
          <Link key={step.title} to={step.to} className="group flex items-center gap-3 rounded-xl border border-slate-100 bg-slate-50/70 p-3 transition hover:border-brand-200 hover:bg-brand-50/40">
            <span className={`flex h-7 w-7 items-center justify-center rounded-full text-xs font-bold ${step.done ? "bg-emerald-100 text-emerald-700" : "bg-white text-slate-500 ring-1 ring-slate-200"}`}>
              {step.done ? <CheckCircle2 size={14} /> : index + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-xs font-semibold text-slate-800">{step.title}</p>
              <p className="truncate text-[11px] text-slate-500">{step.desc}</p>
            </div>
            <ArrowRight size={14} className="text-slate-300 transition-transform group-hover:translate-x-0.5 group-hover:text-brand-500" />
          </Link>
        ))}
      </div>
    </div>
  );
}

export default function Home({ user, resume, toast }) {
  const [overview, setOverview] = useState(null);
  const [applications, setApplications] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.getApplicationsOverview(), api.listApplications()])
      .then(([nextOverview, nextApplications]) => {
        setOverview(nextOverview);
        setApplications(nextApplications);
      })
      .catch((error) => toast(error.message, "error"))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const firstName = user?.email?.split("@")[0]?.split(/[._-]/)[0] || "there";
  const recent = applications.slice(0, 5);
  const completionRate = overview?.total
    ? Math.round(((overview.submitted || 0) / overview.total) * 100)
    : 0;

  return (
    <div className="animate-fade-up space-y-6">
      <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-end">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-brand-600">
            <Sparkles size={13} /> Career command center
          </div>
          <h1 className="page-title capitalize">Good to see you, {firstName}</h1>
          <p className="page-subtitle">Here is what your job-search pipeline needs today.</p>
        </div>
        <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600 shadow-xs">
          <span className="relative flex h-2 w-2"><span className="absolute h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" /><span className="relative h-2 w-2 rounded-full bg-emerald-500" /></span>
          Agent is ready
        </div>
      </div>

      <ApplyFromLink toast={toast} />

      {!resume && (
        <div className="flex flex-col gap-3 rounded-2xl border border-amber-200/80 bg-amber-50/80 px-4 py-3.5 text-sm text-amber-900 sm:flex-row sm:items-center">
          <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-amber-100"><FileWarning size={16} /></span>
          <div className="flex-1"><p className="font-semibold">Your agent is missing a resume</p><p className="text-xs text-amber-700">Upload one to unlock job matching and tailored applications.</p></div>
          <Link to="/resumes" className="inline-flex items-center gap-1.5 text-xs font-bold">Upload resume <ArrowRight size={13} /></Link>
        </div>
      )}

      {loading ? (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">{TILES.map((tile) => <SkeletonTile key={tile.key} />)}</div>
          <div className="grid gap-5 lg:grid-cols-[1.15fr_1.85fr]">
            <div className="card h-64" />
            <div className="card divide-y divide-slate-100 overflow-hidden">{Array.from({ length: 4 }).map((_, index) => <SkeletonRow key={index} />)}</div>
          </div>
        </>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            {TILES.map(({ key, label, icon: Icon, accent, bg }) => (
              <div key={key} className="card group p-5 transition-all hover:-translate-y-0.5 hover:shadow-popover">
                <div className="flex items-start justify-between">
                  <span className={`flex h-9 w-9 items-center justify-center rounded-xl ${bg}`}><Icon size={17} className={accent} /></span>
                  <Activity size={14} className="text-slate-300" />
                </div>
                <p className="mt-4 text-[28px] font-semibold leading-none tracking-[-0.04em] text-slate-950">{overview?.[key] ?? 0}</p>
                <p className="mt-1.5 text-xs font-medium text-slate-500">{label}</p>
              </div>
            ))}
          </div>

          <div className="grid gap-5 xl:grid-cols-[1fr_1.75fr_1fr]">
            <div className="space-y-5">
              <div className="card p-5">
                <div className="mb-5 flex items-center justify-between">
                  <div><h2 className="text-sm font-semibold text-slate-900">Pipeline health</h2><p className="mt-0.5 text-[11px] text-slate-500">Across all applications</p></div>
                  <span className="flex items-center gap-1 text-xs font-semibold text-emerald-600"><TrendingUp size={13} /> {completionRate}%</span>
                </div>
                <StatusBreakdown overview={overview} />
              </div>
              {(!resume || applications.length === 0) && <GettingStarted resumeReady={Boolean(resume)} hasApplied={applications.length > 0} />}
            </div>

            <div className="card overflow-hidden">
              <div className="card-header">
                <div><h2 className="text-sm font-semibold text-slate-900">Recent applications</h2><p className="mt-0.5 text-[11px] text-slate-500">Your latest automation activity</p></div>
                <Link to="/applications" className="flex items-center gap-1 text-xs font-semibold text-brand-600 hover:text-brand-700">View all <ArrowRight size={12} /></Link>
              </div>
              {recent.length === 0 ? (
                <div className="flex min-h-64 flex-col items-center justify-center p-10 text-center">
                  <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100"><Target size={22} className="text-slate-400" /></span>
                  <p className="text-sm font-semibold text-slate-700">Your pipeline is ready</p>
                  <p className="mt-1 max-w-xs text-xs leading-relaxed text-slate-500">Paste a job link above to launch your first assisted application.</p>
                </div>
              ) : (
                <div className="divide-y divide-slate-100">
                  {recent.map((application) => (
                    <Link key={application.application_id} to={`/applications/${application.application_id}`} className="group flex items-center justify-between gap-3 px-5 py-3.5 transition-colors hover:bg-slate-50/80">
                      <div className="flex min-w-0 items-center gap-3">
                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-slate-100 to-slate-200 text-xs font-bold text-slate-600">{(application.company || "?")[0]?.toUpperCase()}</div>
                        <div className="min-w-0"><p className="truncate text-sm font-semibold text-slate-800">{application.position || "Untitled role"}</p><p className="truncate text-xs text-slate-500">{application.company || "Unknown company"}</p></div>
                      </div>
                      <StatusBadge status={application.display_status} />
                    </Link>
                  ))}
                </div>
              )}
            </div>

            <div className="card h-fit p-5">
              <div className="mb-4"><h2 className="text-sm font-semibold text-slate-900">Quick actions</h2><p className="mt-0.5 text-[11px] text-slate-500">Keep your search moving</p></div>
              <div className="space-y-2">
                {QUICK_LINKS.map(({ to, label, desc, icon: Icon, color }) => (
                  <Link key={to} to={to} className="group flex items-center gap-3 rounded-xl border border-transparent p-2.5 transition hover:border-slate-100 hover:bg-slate-50">
                    <span className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl ${color}`}><Icon size={16} /></span>
                    <div className="min-w-0 flex-1"><p className="text-xs font-semibold text-slate-800">{label}</p><p className="truncate text-[11px] text-slate-500">{desc}</p></div>
                    <ArrowUpRight size={14} className="text-slate-300 transition group-hover:text-brand-500" />
                  </Link>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
