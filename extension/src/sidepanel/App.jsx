import { useEffect, useState } from "react";
import { sendMessage, onProgress } from "./lib/chrome.js";

const STATUS_STYLE = {
  applied: "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/25",
  copilot_review: "bg-sky-500/15 text-sky-300 ring-1 ring-sky-400/25",
  needs_review: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/25",
  manual_required: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-400/25",
  failed: "bg-rose-500/15 text-rose-300 ring-1 ring-rose-400/25",
};

const STATUS_LABEL = {
  applied: "Submitted",
  copilot_review: "Ready for your review",
  needs_review: "Needs your review",
  manual_required: "Waiting on you",
  failed: "Failed",
};

function StatusChip({ status }) {
  return (
    <span className={`chip ${STATUS_STYLE[status] || "bg-white/[0.06] text-slate-400"}`}>
      {STATUS_LABEL[status] || status}
    </span>
  );
}

function LoginView({ onLoggedIn, toast }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  async function login() {
    if (!email.trim() || !password) return toast("Enter your email and password.", "error");
    setBusy(true);
    try {
      await sendMessage("LOGIN", { email: email.trim(), password });
      onLoggedIn();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="glass animate-fade-up p-6">
      <h1 className="grad-text text-xl font-bold">Autogram</h1>
      <p className="mb-5 mt-1 text-sm text-slate-400">Sign in with your Autogram account to fill applications from this tab.</p>
      <div className="space-y-3">
        <input className="input-dark" type="email" placeholder="Email" value={email}
          onChange={(e) => setEmail(e.target.value)} onKeyDown={(e) => e.key === "Enter" && login()} />
        <input className="input-dark" type="password" placeholder="Password" value={password}
          onChange={(e) => setPassword(e.target.value)} onKeyDown={(e) => e.key === "Enter" && login()} />
        <button className="btn-primary w-full justify-center" onClick={login} disabled={busy}>
          {busy ? "Signing in..." : "Sign In"}
        </button>
      </div>
    </div>
  );
}

function AutomationStatusBanner({ config, onRefresh }) {
  if (!config) return null;
  if (config.kill_switch_engaged) {
    return (
      <div className="glass border-rose-400/25 bg-rose-500/[0.06] p-4 text-sm">
        <p className="font-semibold text-rose-300">Autopilot kill switch is engaged</p>
        <p className="mt-1 text-xs text-rose-300/80">
          Filling is blocked account-wide until you re-enable autopilot in the web dashboard's Settings page.
        </p>
        <button className="btn-ghost mt-2 !px-3 !py-1.5 text-xs" onClick={onRefresh}>Re-check</button>
      </div>
    );
  }
  return (
    <div className="glass p-3 text-xs text-slate-500">
      <span className="text-slate-400">Auto-submit bar:</span> confidence ≥ {Math.round(config.auto_submit_confidence_threshold * 100)}%
      on {config.public_ats_platforms.join(", ")} · working hours {config.pacing?.working_hours_start}:00–{config.pacing?.working_hours_end}:00
      · daily cap {config.pacing?.daily_application_cap}
    </div>
  );
}

function MainView({ toast }) {
  const [automationConfig, setAutomationConfig] = useState(null);
  const [progress, setProgress] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settings, setSettings] = useState({ backendUrl: "", frontendUrl: "", autopilotEnabled: false, email: "" });

  async function refreshAutomationConfig() {
    try {
      const { config } = await sendMessage("GET_AUTOMATION_CONFIG");
      setAutomationConfig(config);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  useEffect(() => {
    refreshAutomationConfig();
    sendMessage("GET_CONFIG").then(({ config }) => setSettings((s) => ({ ...s, ...config })));
    return onProgress(setProgress);
  }, []);

  async function fill() {
    setBusy(true);
    setResult(null);
    setProgress("Starting...");
    try {
      const { result } = await sendMessage("FILL_THIS_APPLICATION");
      setResult(result);
      toast("Done — see the result below.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
      setProgress("");
      refreshAutomationConfig();
    }
  }

  async function saveSettings() {
    await sendMessage("SET_CONFIG", { config: settings });
    toast("Settings saved.", "success");
    setSettingsOpen(false);
  }

  async function logout() {
    await sendMessage("LOGOUT");
    window.location.reload();
  }

  return (
    <div className="space-y-4 p-4">
      <div className="flex items-center justify-between">
        <h1 className="grad-text text-lg font-bold">Autogram</h1>
        <button className="text-xs text-slate-500 hover:text-slate-300" onClick={() => setSettingsOpen((v) => !v)}>
          {settingsOpen ? "Close" : "Settings"}
        </button>
      </div>

      {settingsOpen ? (
        <div className="glass animate-fade-up space-y-3 p-4">
          <label className="block text-xs text-slate-500">
            Backend URL
            <input className="input-dark mt-1" value={settings.backendUrl}
              onChange={(e) => setSettings((s) => ({ ...s, backendUrl: e.target.value }))} />
          </label>
          <label className="block text-xs text-slate-500">
            Dashboard URL
            <input className="input-dark mt-1" value={settings.frontendUrl}
              onChange={(e) => setSettings((s) => ({ ...s, frontendUrl: e.target.value }))} />
          </label>
          <label className="flex items-center gap-2 text-xs text-slate-400">
            <input type="checkbox" checked={settings.autopilotEnabled}
              onChange={(e) => setSettings((s) => ({ ...s, autopilotEnabled: e.target.checked }))} />
            Enable autopilot (auto-submit when the server approves it)
          </label>
          <div className="flex gap-2">
            <button className="btn-primary !px-3 !py-1.5 text-xs" onClick={saveSettings}>Save</button>
            <button className="btn-ghost !px-3 !py-1.5 text-xs" onClick={logout}>Sign out</button>
          </div>
        </div>
      ) : (
        <>
          <AutomationStatusBanner config={automationConfig} onRefresh={refreshAutomationConfig} />

          <div className={`glass p-5 ${busy ? "glass-active" : ""}`}>
            <p className="mb-3 text-sm text-slate-400">
              Open a job posting in this tab, then fill it using your Autogram profile.
            </p>
            <button className="btn-primary w-full justify-center" onClick={fill} disabled={busy}>
              {busy ? "Working..." : "Fill This Application"}
            </button>
            {busy && (
              <div className="mt-3">
                <div className="shimmer h-1.5 rounded-full" />
                <p className="mt-2 text-center text-xs text-indigo-300">{progress}</p>
              </div>
            )}
          </div>

          {result && (
            <div className="glass animate-fade-up space-y-2 p-4">
              <div className="flex items-center justify-between">
                <span className="text-sm font-semibold text-white">Result</span>
                <StatusChip status={result.status} />
              </div>
              {result.autoSubmitted && (
                <p className="text-xs text-slate-400">
                  Auto-submitted — the server's decide_action() cleared confidence + public-ATS + autopilot for this one.
                </p>
              )}
              {result.resumeFieldFound && (
                <p className="text-xs text-amber-300">
                  This form has a résumé upload field — please attach it yourself; browsers block scripts from setting a file input.
                </p>
              )}
              {result.lowConfidenceCount > 0 && (
                <p className="text-xs text-amber-300">{result.lowConfidenceCount} field(s) need your review.</p>
              )}
              {result.applicationId && result.frontendUrl && (
                <a className="btn-ghost mt-1 w-full justify-center !py-2 text-xs"
                  href={`${result.frontendUrl}/applications/${result.applicationId}`} target="_blank" rel="noreferrer">
                  Open in Dashboard
                </a>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Toast({ toasts }) {
  if (!toasts.length) return null;
  return (
    <div className="fixed bottom-3 left-3 right-3 z-50 space-y-2">
      {toasts.map((t) => (
        <div key={t.id} className={`animate-fade-up rounded-xl px-3 py-2 text-xs shadow-lg ${
          t.type === "error" ? "bg-rose-500/90 text-white" : "bg-emerald-500/90 text-white"
        }`}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const [loggedIn, setLoggedIn] = useState(null); // null = unknown yet
  const [toasts, setToasts] = useState([]);

  function toast(message, type = "info") {
    const id = `${Date.now()}-${Math.random()}`;
    setToasts((t) => [...t, { id, message, type }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4000);
  }

  useEffect(() => {
    sendMessage("GET_CONFIG").then(({ config }) => setLoggedIn(Boolean(config.token)));
  }, []);

  if (loggedIn === null) return <div className="p-6 text-sm text-slate-500">Loading...</div>;

  return (
    <>
      {loggedIn ? <MainView toast={toast} /> : <LoginView onLoggedIn={() => setLoggedIn(true)} toast={toast} />}
      <Toast toasts={toasts} />
    </>
  );
}
