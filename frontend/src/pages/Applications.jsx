import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { LayoutDashboard, ArrowRight, Search, Square, Trash2 } from "lucide-react";
import { api } from "../api";
import StatusBadge, { STATUS_LABELS } from "../components/StatusBadge";
import { SkeletonLine } from "../components/Skeleton";

//: `display_status` values a user can actually stop — mirrors the backend's
//: `IN_PROGRESS_STATUSES` (`pending`/`processing`/`copilot_review`) through
//: `DISPLAY_STATUS_MAP` (`app/services/application_repository.py`).
const STOPPABLE_STATUSES = new Set(["READY", "IN_PROGRESS", "READY_TO_SUBMIT"]);

export default function Applications({ toast }) {
  const [applications, setApplications] = useState([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");
  const [busyId, setBusyId] = useState(null);

  useEffect(() => {
    api.listApplications()
      .then(setApplications)
      .catch((e) => toast(e.message, "error"))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function stop(applicationId) {
    if (!window.confirm("Stop this application? It will be marked cancelled and can be retried later.")) return;
    setBusyId(applicationId);
    try {
      const updated = await api.stopApplication(applicationId);
      setApplications((prev) => prev.map((a) => (a.application_id === applicationId ? updated : a)));
      toast("Application stopped.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(applicationId) {
    if (!window.confirm("Delete this application? This permanently removes its history and cannot be undone.")) return;
    setBusyId(applicationId);
    try {
      await api.deleteApplication(applicationId);
      setApplications((prev) => prev.filter((a) => a.application_id !== applicationId));
      toast("Application deleted.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusyId(null);
    }
  }

  const statusCounts = useMemo(() => {
    const counts = {};
    for (const a of applications) counts[a.display_status] = (counts[a.display_status] || 0) + 1;
    return counts;
  }, [applications]);

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return applications.filter((a) => {
      if (statusFilter && a.display_status !== statusFilter) return false;
      if (q && !`${a.company || ""} ${a.position || ""}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [applications, statusFilter, search]);

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="page-title">Applications</h1>
        <p className="page-subtitle">
          {loading ? "Loading your application history…" : `Every application the automation has ever touched — ${applications.length} total.`}
        </p>
      </div>

      <div className="card overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-slate-200 p-4 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input className="input !pl-9" placeholder="Search by company or position..."
              value={search} onChange={(e) => setSearch(e.target.value)} disabled={loading} />
          </div>
          <div className="flex flex-wrap gap-1 overflow-x-auto rounded-lg bg-slate-100 p-1">
            <button onClick={() => setStatusFilter("")}
              className={`shrink-0 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                statusFilter === "" ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
              All ({applications.length})
            </button>
            {Object.entries(statusCounts).map(([status, count]) => (
              <button key={status} onClick={() => setStatusFilter(status)}
                className={`shrink-0 rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                  statusFilter === status ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
                {STATUS_LABELS[status] || status} ({count})
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="divide-y divide-slate-100">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="grid grid-cols-5 items-center gap-4 px-5 py-3.5">
                <SkeletonLine width="w-24" />
                <SkeletonLine width="w-32" />
                <SkeletonLine width="w-16" />
                <SkeletonLine width="w-10" />
                <SkeletonLine width="w-20" />
              </div>
            ))}
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center p-12 text-center">
            <LayoutDashboard size={30} className="mb-3 text-slate-300" />
            <p className="font-medium text-slate-700">
              {applications.length === 0 ? "No applications yet" : "No applications match your filters"}
            </p>
            <p className="mt-1 max-w-sm text-xs text-slate-500">
              {applications.length === 0
                ? 'Head to Home and use "Apply from a job link", or find a match on the Job Search page.'
                : "Try clearing the search or status filter."}
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="table-head-row">
                  <th className="px-5 py-2.5 font-semibold">Company</th>
                  <th className="px-5 py-2.5 font-semibold">Position</th>
                  <th className="px-5 py-2.5 font-semibold">Status</th>
                  <th className="px-5 py-2.5 font-semibold">Confidence</th>
                  <th className="px-5 py-2.5 font-semibold">Last Updated</th>
                  <th className="px-5 py-2.5 font-semibold" />
                </tr>
              </thead>
              <tbody>
                {filtered.map((a) => (
                  <tr key={a.application_id} className="table-row">
                    <td className="px-5 py-3 font-medium text-slate-800">{a.company || "—"}</td>
                    <td className="px-5 py-3 text-slate-600">{a.position || "—"}</td>
                    <td className="px-5 py-3"><StatusBadge status={a.display_status} /></td>
                    <td className="px-5 py-3 text-slate-600">
                      {a.confidence_score != null ? `${Math.round(a.confidence_score * 100)}%` : "—"}
                    </td>
                    <td className="px-5 py-3 text-xs text-slate-500">
                      {a.updated_at ? new Date(a.updated_at).toLocaleString() : "—"}
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center justify-end gap-1.5">
                        {STOPPABLE_STATUSES.has(a.display_status) && (
                          <button onClick={() => stop(a.application_id)} disabled={busyId === a.application_id}
                            title="Stop this application"
                            className="btn-ghost !px-2.5 !py-1.5 text-xs text-amber-700 hover:bg-amber-50 disabled:opacity-50">
                            <Square size={13} />
                          </button>
                        )}
                        <button onClick={() => remove(a.application_id)} disabled={busyId === a.application_id}
                          title="Delete this application"
                          className="btn-ghost !px-2.5 !py-1.5 text-xs text-red-600 hover:bg-red-50 disabled:opacity-50">
                          <Trash2 size={13} />
                        </button>
                        <Link to={`/applications/${a.application_id}`}
                          className="btn-ghost !px-3 !py-1.5 text-xs">
                          View <ArrowRight size={13} />
                        </Link>
                      </div>
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
