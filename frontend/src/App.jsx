import { useEffect, useState, useCallback } from "react";
import { Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import {
  CheckCircle2, AlertCircle, Info, X, LogOut, Loader2,
  Home as HomeIcon, Search, LayoutDashboard, UserCircle2, FileText, Settings as SettingsIcon,
  Bot, Menu, Sparkles, PanelLeftClose, Gauge,
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
import AutonomousAgent from "./pages/AutonomousAgent";
import Metrics from "./pages/Metrics";

// Grouped so the sidebar reads as "what you do" vs "how it's set up",
// instead of one flat list of seven equally-weighted items.
const NAV_GROUPS = [
  {
    label: "Workspace",
    links: [
      { to: "/", label: "Home", icon: HomeIcon, end: true },
      { to: "/search", label: "Job Search", icon: Search },
      { to: "/applications", label: "Applications", icon: LayoutDashboard },
      { to: "/agent", label: "Autonomous Agent", icon: Bot },
      { to: "/metrics", label: "Success Metrics", icon: Gauge },
    ],
  },
  {
    label: "Account",
    links: [
      { to: "/profile", label: "Profile", icon: UserCircle2 },
      { to: "/resumes", label: "Resumes", icon: FileText },
      { to: "/settings", label: "Settings", icon: SettingsIcon },
    ],
  },
];
const NAV_LINKS = NAV_GROUPS.flatMap((g) => g.links);

let toastId = 0;

export default function App() {
  const [user, setUser] = useState(null);
  const [authChecking, setAuthChecking] = useState(true);
  const [resume, setResume] = useState(null);
  const [health, setHealth] = useState(null);
  const [toasts, setToasts] = useState([]);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const location = useLocation();

  useEffect(() => {
    setMobileMenuOpen(false);
    window.scrollTo(0, 0);
    const page = NAV_LINKS.find((item) => item.end ? location.pathname === item.to : location.pathname.startsWith(item.to));
    document.title = `${page?.label || "Workspace"} · Autogram`;
  }, [location.pathname]);

  useEffect(() => {
    if (location.hash) requestAnimationFrame(() => document.getElementById(location.hash.slice(1))?.scrollIntoView());
  }, [location.pathname, location.hash]);

  useEffect(() => {
    if (!mobileMenuOpen) return;
    const close = (event) => { if (event.key === "Escape") setMobileMenuOpen(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [mobileMenuOpen]);

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
  const currentPage = NAV_LINKS.find((item) => item.end ? location.pathname === item.to : location.pathname.startsWith(item.to));

  return (
    <div className="app-shell flex min-h-screen">
      <a href="#main-content" className="skip-link">Skip to content</a>
      {/* Sidebar */}
      <aside className="sidebar fixed inset-y-0 left-0 z-40 hidden w-[272px] flex-col lg:flex">
        <div className="flex h-[76px] items-center gap-3 border-b border-white/[0.07] px-6">
          <div className="brand-mark">
            <Sparkles size={17} className="text-white" />
          </div>
          <div>
            <span className="block text-[17px] font-semibold tracking-[-0.03em] text-white">autogram</span>
            <span className="block text-[9px] font-semibold uppercase tracking-[0.2em] text-slate-500">Career OS</span>
          </div>
        </div>

        <nav className="flex-1 space-y-6 overflow-y-auto px-3 py-6">
          {NAV_GROUPS.map((group) => (
            <div key={group.label}>
              <p className="mb-2 px-3 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-500">{group.label}</p>
              <div className="space-y-1">
                {group.links.map(({ to, label, icon: Icon, end }) => (
                  <NavLink key={to} to={to} end={end}
                    className={({ isActive }) =>
                      `group flex items-center gap-3 rounded-xl px-3 py-2.5 text-[13px] font-medium transition-all ${
                        isActive
                          ? "bg-white/[0.09] text-white shadow-[inset_0_0_0_1px_rgba(255,255,255,0.05)]"
                          : "text-slate-400 hover:bg-white/[0.05] hover:text-slate-100"
                      }`
                    }
                  >
                    {({ isActive }) => (
                      <>
                        <span className={`flex h-7 w-7 items-center justify-center rounded-lg transition-colors ${isActive ? "bg-brand-500 text-white" : "text-slate-500 group-hover:text-slate-300"}`}>
                          <Icon size={15} />
                        </span>
                        {label}
                        {to === "/agent" && <span className="ml-auto rounded-full bg-cyan-400/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-cyan-300">AI</span>}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-white/[0.07] p-4">
          <div className="mb-3 flex items-center gap-2 rounded-lg px-2 text-[11px] text-slate-500">
            <span className="relative flex h-1.5 w-1.5 shrink-0">
              {health?.status === "ok" && (
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              )}
              <span className={`relative inline-flex h-1.5 w-1.5 rounded-full ${healthColor}`} />
            </span>
            <span className="truncate">{healthLabel}</span>
          </div>
          <div className="flex items-center gap-2.5 rounded-xl border border-white/[0.07] bg-white/[0.04] p-2.5">
            <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-brand-400 to-violet-500 text-xs font-semibold text-white">
              {user.email?.[0]?.toUpperCase()}
            </div>
            <div className="min-w-0 flex-1">
              <span className="block truncate text-xs font-medium text-slate-200">{user.email?.split("@")[0]}</span>
              <span className="block truncate text-[10px] text-slate-500">Personal workspace</span>
            </div>
            <button onClick={logout} title="Log out" className="rounded-lg p-1.5 text-slate-500 transition hover:bg-white/10 hover:text-white">
              <LogOut size={14} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main column */}
      <div className="flex min-h-screen w-full flex-1 flex-col lg:pl-[272px]">
        <header className="topbar sticky top-0 z-30 flex h-[76px] items-center justify-between px-4 sm:px-8">
          <div className="flex items-center gap-3">
            <button onClick={() => setMobileMenuOpen((value) => !value)} className="btn-icon lg:hidden" aria-label="Toggle navigation" aria-expanded={mobileMenuOpen} aria-controls="mobile-navigation">
              {mobileMenuOpen ? <PanelLeftClose size={19} /> : <Menu size={19} />}
            </button>
            <div className="flex items-center gap-2 lg:hidden">
              <div className="brand-mark !h-8 !w-8 !rounded-[10px]">
                <Sparkles size={14} className="text-white" />
              </div>
              <span className="font-semibold tracking-tight text-slate-900">autogram</span>
            </div>
            <div className="hidden items-center gap-2 text-sm lg:flex">
              <span className="text-slate-400">Workspace</span>
              <span className="text-slate-300">/</span>
              <span className="font-medium text-slate-700">{currentPage?.label || "Autogram"}</span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Link to="/#apply-from-link" className="btn-primary !py-2 text-xs">+ New application</Link>
            {resume && <div className="hidden items-center gap-2 rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-[11px] font-medium text-emerald-700 sm:flex"><CheckCircle2 size={13} /> Resume ready</div>}
            <button className="hidden items-center gap-2 rounded-xl border border-slate-200 bg-white px-2.5 py-1.5 text-xs font-medium text-slate-700 shadow-xs sm:flex lg:hidden" onClick={logout} title="Log out">
              <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-brand-50 font-bold text-brand-700">{user.email?.[0]?.toUpperCase()}</span>
              <LogOut size={13} className="text-slate-400" />
            </button>
          </div>
        </header>

        {mobileMenuOpen && (
          <nav id="mobile-navigation" aria-label="Mobile navigation" className="fixed inset-x-3 top-[76px] z-50 grid max-h-[calc(100dvh-90px)] gap-1 overflow-y-auto rounded-2xl border border-slate-200 bg-white p-2 shadow-popover lg:hidden">
            {NAV_LINKS.map(({ to, label, icon: Icon, end }) => (
              <NavLink key={to} to={to} end={end} onClick={() => setMobileMenuOpen(false)} className={({ isActive }) => `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium ${isActive ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-50"}`}>
                <Icon size={16} /> {label}
              </NavLink>
            ))}
            <button onClick={logout} className="mt-1 flex items-center gap-3 border-t border-slate-100 px-3 pt-3 pb-2 text-left text-sm font-medium text-slate-500 hover:text-slate-900">
              <LogOut size={16} /> Log out
            </button>
          </nav>
        )}

        <main id="main-content" tabIndex={-1} className="mx-auto min-w-0 w-full max-w-[1440px] flex-1 px-4 py-7 sm:px-8 lg:px-10 lg:py-9">
          <Routes>
            <Route path="/" element={<Home user={user} resume={resume} toast={toast} />} />
            <Route path="/search" element={<JobsAndMatches resume={resume} setResume={setResume} toast={toast} />} />
            <Route path="/applications" element={<Applications toast={toast} />} />
            <Route path="/applications/:id" element={<ApplicationDetail toast={toast} />} />
            <Route path="/agent" element={<AutonomousAgent toast={toast} />} />
            <Route path="/agent/:id" element={<AutonomousAgent toast={toast} />} />
            <Route path="/metrics" element={<Metrics toast={toast} />} />
            <Route path="/profile" element={<Profile toast={toast} />} />
            <Route path="/resumes" element={<ResumeManagement toast={toast} />} />
            <Route path="/settings" element={<Settings toast={toast} />} />
            <Route path="*" element={<div className="empty-state"><h1 className="page-title">Page not found</h1><p className="page-subtitle">Let’s get you back to your workspace.</p><Link to="/" className="btn-primary mt-6">Back to home</Link></div>} />
          </Routes>
        </main>
      </div>

      {/* Toasts */}
      <div aria-live="polite" className="fixed bottom-5 right-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2.5">
        {toasts.map((t) => (
          <div key={t.id}
            className="animate-slide-in relative flex items-start gap-2.5 overflow-hidden rounded-xl border border-slate-200 bg-white p-3.5 text-sm shadow-popover">
            <div className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${
              t.type === "success" ? "bg-emerald-50" : t.type === "error" ? "bg-red-50" : "bg-brand-50"}`}>
              {t.type === "success" && <CheckCircle2 size={13} className="text-emerald-600" />}
              {t.type === "error" && <AlertCircle size={13} className="text-red-600" />}
              {t.type === "info" && <Info size={13} className="text-brand-600" />}
            </div>
            <span className="flex-1 pt-0.5 leading-snug text-slate-700">{t.message}</span>
            <button aria-label="Dismiss notification" onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}
              className="text-slate-400 transition hover:text-slate-700">
              <X size={14} />
            </button>
            <div className="absolute inset-x-0 bottom-0 h-0.5 bg-slate-100">
              <div
                className={`h-full ${t.type === "success" ? "bg-emerald-500" : t.type === "error" ? "bg-red-500" : "bg-brand-500"}`}
                style={{ animation: "toastShrink 4.5s linear forwards" }}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
