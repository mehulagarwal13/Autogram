import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  LayoutDashboard, CheckCircle2, Loader2, AlertTriangle, Clock, XCircle, Ban, ArrowRight,
} from "lucide-react";
import { api } from "../api";
import ApplyFromLink from "../components/ApplyFromLink";
import StatusBadge from "../components/StatusBadge";

const TILES = [
  { key: "total", label: "Total Applications", icon: LayoutDashboard, color: "text-indigo-300" },
  { key: "submitted", label: "Submitted", icon: CheckCircle2, color: "text-emerald-300" },
  { key: "in_progress", label: "In Progress", icon: Loader2, color: "text-sky-300" },
  { key: "waiting_for_human", label: "Waiting for You", icon: AlertTriangle, color: "text-amber-300" },
  { key: "waiting_for_review", label: "Needs Review", icon: Clock, color: "text-amber-300" },
  { key: "failed", label: "Failed", icon: XCircle, color: "text-rose-300" },
  { key: "cancelled", label: "Cancelled", icon: Ban, color: "text-slate-400" },
];

export default function Dashboard({ toast }) {
  const [overview, setOverview] = useState(null);
  const [applications, setApplications] = useState([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    try {
      const [ov, apps] = await Promise.all([api.getApplicationsOverview(), api.listApplications()]);
      setOverview(ov);
      setApplications(apps);
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return (
      <div className="flex justify-center py-20">
        <Loader2 size={28} className="animate-spin text-indigo-400" />
      </div>
    );
  }

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="text-xl font-bold text-white">Application Dashboard</h1>
        <p className="text-sm text-slate-500">Every application the automation has ever touched, in one place.</p>
      </div>

      <ApplyFromLink toast={toast} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        {TILES.map((t) => {
          const Icon = t.icon;
          return (
            <div key={t.key} className="glass p-4">
              <Icon size={18} className={t.color} />
              <p className="mt-2 text-2xl font-bold text-white">{overview?.[t.key] ?? 0}</p>
              <p className="text-[11px] text-slate-500">{t.label}</p>
            </div>
          );
        })}
      </div>

      <div className="glass overflow-hidden">
        <div className="border-b border-white/[0.06] px-5 py-4">
          <h2 className="font-semibold text-white">All Applications</h2>
        </div>

        {applications.length === 0 ? (
          <div className="flex flex-col items-center p-12 text-center">
            <LayoutDashboard size={32} className="mb-3 text-slate-600" />
            <p className="font-semibold text-slate-400">No applications yet</p>
            <p className="mt-1 max-w-sm text-xs text-slate-600">
              Find a match on the Jobs page and hit "Start Application" to see it here.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-white/[0.06] text-[11px] uppercase tracking-wide text-slate-500">
                  <th className="px-5 py-2.5 font-medium">Company</th>
                  <th className="px-5 py-2.5 font-medium">Position</th>
                  <th className="px-5 py-2.5 font-medium">Status</th>
                  <th className="px-5 py-2.5 font-medium">Confidence</th>
                  <th className="px-5 py-2.5 font-medium">Last Updated</th>
                  <th className="px-5 py-2.5 font-medium" />
                </tr>
              </thead>
              <tbody>
                {applications.map((a) => (
                  <tr key={a.application_id} className="border-b border-white/[0.04] transition hover:bg-white/[0.02]">
                    <td className="px-5 py-3 font-medium text-slate-200">{a.company || "—"}</td>
                    <td className="px-5 py-3 text-slate-400">{a.position || "—"}</td>
                    <td className="px-5 py-3"><StatusBadge status={a.display_status} /></td>
                    <td className="px-5 py-3 text-slate-400">
                      {a.confidence_score != null ? `${Math.round(a.confidence_score * 100)}%` : "—"}
                    </td>
                    <td className="px-5 py-3 text-xs text-slate-500">
                      {a.updated_at ? new Date(a.updated_at).toLocaleString() : "—"}
                    </td>
                    <td className="px-5 py-3 text-right">
                      <Link to={`/applications/${a.application_id}`}
                        className="btn-ghost !px-3 !py-1.5 text-xs">
                        View <ArrowRight size={13} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
