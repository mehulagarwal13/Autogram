import { useState } from "react";
import { BrainCircuit, Mail, Lock, Loader2, LogIn, UserPlus } from "lucide-react";
import { api, auth } from "../api";

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
    <div className="flex min-h-screen items-center justify-center bg-slate-50 px-4">
      <div className="w-full max-w-sm">
        {/* Brand */}
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-brand-600">
            <BrainCircuit size={22} className="text-white" />
          </div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">Autogram</h1>
          <p className="mt-1 text-sm text-slate-500">
            Your resume, matched against the live job market.
          </p>
        </div>

        <div className="card animate-fade-up p-7">
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
              <input className="input !pl-10" type="email" placeholder="you@example.com"
                value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
            </div>
            <div className="relative">
              <Lock size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input className="input !pl-10" type="password"
                placeholder={mode === "signup" ? "Password (min 8 characters)" : "Password"}
                value={password} onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === "signup" ? "new-password" : "current-password"} />
            </div>

            {error && (
              <p className="animate-fade-up rounded-lg border border-red-200 bg-red-50 px-3.5 py-2.5 text-xs text-red-700">
                {error}
              </p>
            )}

            <button type="submit" className="btn-primary w-full" disabled={busy}>
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
  );
}
