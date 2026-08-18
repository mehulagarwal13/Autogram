import { useEffect, useState, useCallback } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import {
  BrainCircuit, CheckCircle2, AlertCircle, Info, X, LogOut, Loader2,
  Home as HomeIcon, Search, LayoutDashboard, UserCircle2, FileText, Settings as SettingsIcon,
} from "lucide-react";
import { api, auth, setUnauthorizedHandler } from "./api";
import AuthPage from "./components/AuthPage";
import Home from "./pages/Home";
import JobsAndMatches from "./pages/JobsAndMatches";
import Applications from "./pages/Applications";
import ApplicationDetail from "./pages/ApplicationDetail";
import Profile from "./pages/Profile";
import ResumeManagement from "./pages/ResumeManagement";
import Settings from "./pages/Settings";

const NAV_LINKS = [
  { to: "/", label: "Home", icon: HomeIcon, end: true },
  { to: "/search", label: "Job Search", icon: Search },
  { to: "/applications", label: "Applications", icon: LayoutDashboard },
  { to: "/profile", label: "Profile", icon: UserCircle2 },
  { to: "/resumes", label: "Resumes", icon: FileText },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
];

let toastId = 0;

export default function App() {
  const [user, setUser] = useState(null);
  const [authChecking, setAuthChecking] = useState(true);
  const [resume, setResume] = useState(null);
  const [health, setHealth] = useState(null);
  const [toasts, setToasts] = useState([]);

  const toast = useCallback((message, type = "info") => {
    const id = ++toastId;
    setToasts((t) => [...t, { id, message, type }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4500);
  }, []);

  const logout = useCallback(() => {
    auth.clear();
    setUser(null);
    setResume(null);
  }, []);

  // Restore session: validate stored token, then pull the user's latest resume
  useEffect(() => {
    setUnauthorizedHandler(() => { setUser(null); setResume(null); });
    if (!auth.getToken()) {
      setAuthChecking(false);
      return;
    }
    api.me()
      .then((me) => {
        setUser({ email: me.email });
        return api.listResumes();
      })
      .then((r) => {
        const latest = r?.resumes?.[0];
        if (latest) setResume({ id: latest.resume_id, filename: latest.original_filename });
      })
      .catch(() => auth.clear())
      .finally(() => setAuthChecking(false));
  }, []);

  useEffect(() => {
    const check = () => api.health().then(setHealth).catch(() => setHealth({ status: "down" }));
    check();
    const interval = setInterval(check, 30000);
    return () => clearInterval(interval);
  }, []);

  async function handleAuthed(u) {
    setUser(u);
    try {
      const r = await api.listResumes();
      const latest = r?.resumes?.[0];
      if (latest) {
        setResume({ id: latest.resume_id, filename: latest.original_filename });
        toast(`Welcome back — restored "${latest.original_filename}".`, "info");
      }
    } catch { /* fresh account, nothing to restore */ }
  }

  if (authChecking) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50">
        <Loader2 size={26} className="animate-spin text-brand-600" />
      </div>
    );
  }

  if (!user) return <AuthPage onAuthed={handleAuthed} />;

  const healthColor =
    health?.status === "ok" ? "bg-emerald-500" : health?.status === "degraded" ? "bg-amber-500" : "bg-red-500";
  const healthLabel =
    health?.status === "ok" ? "All systems operational" : health?.status === "degraded" ? "Database unreachable" : "Offline";

  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="fixed inset-y-0 left-0 hidden w-60 flex-col border-r border-slate-200 bg-white lg:flex">
        <div className="flex items-center gap-2.5 px-5 py-5">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600">
            <BrainCircuit size={18} className="text-white" />
          </div>
          <span className="text-base font-semibold tracking-tight text-slate-900">Autogram</span>
        </div>

        <nav className="flex-1 space-y-0.5 px-3 py-2">
          {NAV_LINKS.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                  isActive ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                }`
              }
            >
              <Icon size={16} /> {label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-slate-200 p-3">
          <div className="mb-2 flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs text-slate-500">
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${healthColor}`} />
            <span className="truncate">{healthLabel}</span>
          </div>
          <div className="flex items-center gap-2.5 rounded-lg px-2 py-1.5">
            <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold text-slate-600">
              {user.email?.[0]?.toUpperCase()}
            </div>
            <span className="min-w-0 flex-1 truncate text-xs font-medium text-slate-700">{user.email}</span>
            <button onClick={logout} title="Log out" className="btn-icon shrink-0">
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main column */}
      <div className="flex min-h-screen w-full flex-1 flex-col lg:pl-60">
        {/* Mobile top bar */}
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 lg:hidden">
          <div className="flex items-center gap-2">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-brand-600">
              <BrainCircuit size={15} className="text-white" />
            </div>
            <span className="font-semibold text-slate-900">Autogram</span>
          </div>
          <button onClick={logout} className="btn-icon"><LogOut size={16} /></button>
        </header>

        <nav className="flex gap-1 overflow-x-auto border-b border-slate-200 bg-white px-3 py-2 lg:hidden">
          {NAV_LINKS.map(({ to, label, icon: Icon, end }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) =>
                `flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium ${
                  isActive ? "bg-brand-50 text-brand-700" : "text-slate-500"
                }`
              }
            >
              <Icon size={14} /> {label}
            </NavLink>
          ))}
        </nav>

        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-8">
          <Routes>
            <Route path="/" element={<Home resume={resume} toast={toast} />} />
            <Route path="/search" element={<JobsAndMatches resume={resume} setResume={setResume} toast={toast} />} />
            <Route path="/applications" element={<Applications toast={toast} />} />
            <Route path="/applications/:id" element={<ApplicationDetail toast={toast} />} />
            <Route path="/profile" element={<Profile toast={toast} />} />
            <Route path="/resumes" element={<ResumeManagement toast={toast} />} />
            <Route path="/settings" element={<Settings toast={toast} />} />
          </Routes>
        </main>
      </div>

      {/* Toasts */}
      <div className="fixed bottom-5 right-5 z-[60] flex w-80 flex-col gap-2">
        {toasts.map((t) => (
          <div key={t.id}
            className={`card animate-fade-up flex items-start gap-2.5 border-l-4 p-3.5 text-sm ${
              t.type === "success" ? "border-l-emerald-500" :
              t.type === "error" ? "border-l-red-500" : "border-l-brand-500"}`}>
            {t.type === "success" && <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-emerald-600" />}
            {t.type === "error" && <AlertCircle size={16} className="mt-0.5 shrink-0 text-red-600" />}
            {t.type === "info" && <Info size={16} className="mt-0.5 shrink-0 text-brand-600" />}
            <span className="flex-1 leading-snug text-slate-700">{t.message}</span>
            <button onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}
              className="text-slate-400 transition hover:text-slate-700">
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
