import { useState } from "react";
import { BrainCircuit, Mail, Lock, Loader2, LogIn, UserPlus, Target, ShieldCheck, Sparkles } from "lucide-react";
import { api, auth } from "../api";

const VALUE_PROPS = [
  { icon: Target, title: "AI-matched roles", desc: "Every listing scored against your resume — semantic fit, skills, and ATS keywords." },
  { icon: Sparkles, title: "Autonomous applications", desc: "Paste a job link and the agent fills the form, page by page, on its own." },
  { icon: ShieldCheck, title: "You stay in control", desc: "Choose your approval settings. Step in for verification and review anything that needs your input." },
];

export default function AuthPage({ onAuthed }) {
  const [mode, setMode] = useState("login"); // "login" | "signup"
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    if (!email || !password) return setError("Enter your email and password.");
    setBusy(true);
    setError("");
    try {
      const r = mode === "signup"
        ? await api.signup(email, password)
        : await api.login(email, password);
      auth.setToken(r.access_token);
      onAuthed({ email: r.email });
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen bg-white">
      {/* Brand / value-prop panel */}
      <div className="relative hidden w-[46%] max-w-xl flex-col justify-between overflow-hidden bg-brand-gradient px-12 py-12 text-white lg:flex">
        <div
          className="pointer-events-none absolute inset-0 opacity-[0.07]"
          style={{
            backgroundImage:
              "radial-gradient(circle at 1px 1px, white 1px, transparent 0)",
            backgroundSize: "28px 28px",
          }}
        />
        <div className="relative flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-white/15 ring-1 ring-white/20 backdrop-blur">
            <BrainCircuit size={19} />
          </div>
          <span className="text-lg font-semibold tracking-tight">Autogram</span>
        </div>

        <div className="relative">
          <h1 className="text-3xl font-semibold leading-tight tracking-tight">
            One resume.<br />A world of possibility.
          </h1>
          <p className="mt-3 max-w-md text-sm leading-relaxed text-white/75">
            Autogram sources real listings, ranks them against your profile, and drives the
            application itself — so your time goes into interviews, not forms.
          </p>

          <div className="mt-10 space-y-6">
            {VALUE_PROPS.map(({ icon: Icon, title, desc }) => (
              <div key={title} className="flex items-start gap-3.5">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-white/10 ring-1 ring-white/15">
                  <Icon size={16} />
                </div>
                <div>
                  <p className="text-sm font-semibold">{title}</p>
                  <p className="mt-0.5 text-xs leading-relaxed text-white/70">{desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <p className="relative text-xs text-white/50">© {new Date().getFullYear()} Autogram</p>
      </div>

      {/* Form panel */}
      <div className="flex flex-1 items-center justify-center bg-slate-50 px-4 py-12 lg:bg-white">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex flex-col items-center text-center lg:hidden">
            <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-brand-gradient shadow-xs">
              <BrainCircuit size={22} className="text-white" />
            </div>
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">Autogram</h1>
            <p className="mt-1 text-sm text-slate-500">
              Your resume, matched against the live job market.
            </p>
          </div>

          <div className="mb-6 hidden lg:block">
            <h2 className="text-xl font-semibold tracking-tight text-slate-900">
              {mode === "signup" ? "Create your account" : "Welcome back"}
            </h2>
            <p className="mt-1 text-sm text-slate-500">
              {mode === "signup" ? "Start matching against live listings in minutes." : "Log in to continue your job search."}
            </p>
          </div>

          <div className="card animate-fade-up p-7 lg:border-none lg:p-0 lg:shadow-none">
            {/* Mode switch */}
            <div className="mb-6 flex gap-1 rounded-lg bg-slate-100 p-1">
              {[["login", "Log in"], ["signup", "Create account"]].map(([m, label]) => (
                <button key={m} type="button"
                  onClick={() => { setMode(m); setError(""); }}
                  className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                    mode === m ? "bg-white text-slate-900 shadow-sm" : "text-slate-500 hover:text-slate-700"}`}>
                  {label}
                </button>
              ))}
            </div>

            <form onSubmit={submit} className="space-y-4">
              <div className="relative">
                <Mail size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
                <input className="input !pl-10" type="email" placeholder="you@example.com" aria-label="Email address" required disabled={busy}
                  value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
              </div>
              <div className="relative">
                <Lock size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
                <input className="input !pl-10" type="password" aria-label="Password" required minLength={mode === "signup" ? 8 : undefined} disabled={busy}
                  placeholder={mode === "signup" ? "Password (min 8 characters)" : "Password"}
                  value={password} onChange={(e) => setPassword(e.target.value)}
                  autoComplete={mode === "signup" ? "new-password" : "current-password"} />
              </div>

              {error && (
                <p role="alert" className="animate-fade-up rounded-lg border border-red-200 bg-red-50 px-3.5 py-2.5 text-xs text-red-700">
                  {error}
                </p>
              )}

              <button type="submit" className="btn-primary w-full !py-3" disabled={busy}>
                {busy ? <Loader2 size={16} className="animate-spin" />
                  : mode === "signup" ? <UserPlus size={16} /> : <LogIn size={16} />}
                {busy ? "Please wait..." : mode === "signup" ? "Create account" : "Log in"}
              </button>
            </form>

            <p className="mt-5 text-center text-xs text-slate-500">
              {mode === "login" ? "New here? " : "Already have an account? "}
              <button type="button" className="font-semibold text-brand-600 hover:text-brand-700"
                onClick={() => { setMode(mode === "login" ? "signup" : "login"); setError(""); }}>
                {mode === "login" ? "Create an account" : "Log in instead"}
              </button>
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
