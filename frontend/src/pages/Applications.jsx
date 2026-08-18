import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { LayoutDashboard, Loader2, ArrowRight, Search } from "lucide-react";
import { api } from "../api";
import StatusBadge, { STATUS_LABELS } from "../components/StatusBadge";

export default function Applications({ toast }) {
  const [applications, setApplications] = useState([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    api.listApplications()
      .then(setApplications)
      .catch((e) => toast(e.message, "error"))
      .finally(() => setLoading(false));
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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

  if (loading) {
    return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;
  }

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="page-title">Applications</h1>
        <p className="page-subtitle">Every application the automation has ever touched — {applications.length} total.</p>
      </div>

      <div className="card overflow-hidden">
        <div className="flex flex-col gap-3 border-b border-slate-200 p-4 sm:flex-row sm:items-center">
          <div className="relative flex-1">
            <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input className="input !pl-9" placeholder="Search by company or position..."
              value={search} onChange={(e) => setSearch(e.target.value)} />
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

        {filtered.length === 0 ? (
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
