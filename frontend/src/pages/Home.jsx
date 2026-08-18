import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  LayoutDashboard, CheckCircle2, Loader2, AlertTriangle, XCircle,
  Search, FileText, Settings as SettingsIcon, ArrowRight, ArrowUpRight, FileWarning,
} from "lucide-react";
import { api } from "../api";
import ApplyFromLink from "../components/ApplyFromLink";
import StatusBadge from "../components/StatusBadge";
import StatusBreakdown from "../components/StatusBreakdown";

const TILES = [
  { key: "total", label: "Total Applications", icon: LayoutDashboard, color: "text-slate-500" },
  { key: "submitted", label: "Submitted", icon: CheckCircle2, color: "text-emerald-600" },
  { key: "in_progress", label: "In Progress", icon: Loader2, color: "text-brand-600" },
  { key: "waiting_for_human", label: "Waiting for You", icon: AlertTriangle, color: "text-amber-600" },
  { key: "failed", label: "Failed", icon: XCircle, color: "text-red-600" },
];

const QUICK_LINKS = [
  { to: "/search", label: "Search Jobs", desc: "Find and match new openings", icon: Search },
  { to: "/resumes", label: "Manage Resumes", desc: "Upload variants & cover letters", icon: FileText },
  { to: "/settings", label: "Automation Settings", desc: "Autopilot & safety controls", icon: SettingsIcon },
];

export default function Home({ resume, toast }) {
  const [overview, setOverview] = useState(null);
  const [applications, setApplications] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.getApplicationsOverview(), api.listApplications()])
      .then(([ov, apps]) => { setOverview(ov); setApplications(apps); })
      .catch((e) => toast(e.message, "error"))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const recent = applications.slice(0, 5);

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="page-title">Home</h1>
        <p className="page-subtitle">Your job search, at a glance.</p>
      </div>

      <ApplyFromLink toast={toast} />

      {!resume && (
        <div className="flex items-center gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <FileWarning size={16} className="shrink-0" />
          <span className="flex-1">No resume on file yet — upload one to start getting AI-matched jobs.</span>
          <Link to="/search" className="shrink-0 font-semibold text-amber-900 hover:underline">Upload now</Link>
        </div>
      )}

      {/* Quick links */}
      <div className="grid gap-3 sm:grid-cols-3">
        {QUICK_LINKS.map(({ to, label, desc, icon: Icon }) => (
          <Link key={to} to={to} className="card group flex items-start gap-3 p-4 transition-shadow hover:shadow-popover">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-100 text-slate-600 group-hover:bg-brand-50 group-hover:text-brand-600">
              <Icon size={17} />
            </div>
            <div className="min-w-0 flex-1">
              <p className="flex items-center gap-1 text-sm font-semibold text-slate-900">
                {label} <ArrowUpRight size={13} className="text-slate-400 opacity-0 transition-opacity group-hover:opacity-100" />
              </p>
              <p className="mt-0.5 text-xs text-slate-500">{desc}</p>
            </div>
          </Link>
        ))}
      </div>

      {loading ? (
        <div className="flex justify-center py-16"><Loader2 size={24} className="animate-spin text-brand-600" /></div>
      ) : (
        <>
          {/* KPI tiles */}
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            {TILES.map((t) => {
              const Icon = t.icon;
              return (
                <div key={t.key} className="card p-4">
                  <Icon size={17} className={t.color} />
                  <p className="mt-2 text-2xl font-semibold text-slate-900">{overview?.[t.key] ?? 0}</p>
                  <p className="text-[11px] text-slate-500">{t.label}</p>
                </div>
              );
            })}
          </div>

          <div className="grid gap-5 lg:grid-cols-5">
            {/* Status breakdown */}
            <div className="card p-6 lg:col-span-2">
              <h2 className="mb-4 font-semibold text-slate-900">Application Breakdown</h2>
              <StatusBreakdown overview={overview} />
            </div>

            {/* Recent applications */}
            <div className="card overflow-hidden lg:col-span-3">
              <div className="card-header">
                <h2 className="font-semibold text-slate-900">Recent Applications</h2>
                <Link to="/applications" className="flex items-center gap-1 text-xs font-medium text-brand-600 hover:text-brand-700">
                  View all <ArrowRight size={12} />
                </Link>
              </div>
              {recent.length === 0 ? (
                <div className="flex flex-col items-center p-10 text-center">
                  <LayoutDashboard size={28} className="mb-2 text-slate-300" />
                  <p className="text-sm text-slate-500">No applications yet — start one above.</p>
                </div>
              ) : (
                <div className="divide-y divide-slate-100">
                  {recent.map((a) => (
                    <Link key={a.application_id} to={`/applications/${a.application_id}`}
                      className="flex items-center justify-between gap-3 px-5 py-3 transition-colors hover:bg-slate-50">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-slate-800">{a.position || "—"}</p>
                        <p className="truncate text-xs text-slate-500">{a.company || "Unknown company"}</p>
                      </div>
                      <StatusBadge status={a.display_status} />
                    </Link>
                  ))}
                </div>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
