import { useEffect, useState } from "react";
import { ShieldOff, ShieldCheck, Loader2 } from "lucide-react";
import { api } from "../api";

export default function Settings({ toast }) {
  const [profile, setProfile] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getProfile().then(setProfile).catch(() => setProfile(null)).finally(() => setLoading(false));
  }, []);

  async function toggleKillSwitch() {
    if (!profile) return;
    const next = !profile.autopilot_globally_disabled;
    if (next && !window.confirm("This will hard-stop every autopilot run for your account, even ones already in progress. Continue?")) return;
    setSaving(true);
    try {
      const updated = await api.updateAutomationSettings({ autopilot_globally_disabled: next });
      setProfile((p) => ({ ...p, ...updated }));
      toast(next ? "Autopilot disabled account-wide." : "Autopilot re-enabled.", next ? "info" : "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;

  const disabled = !!profile?.autopilot_globally_disabled;

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="page-title">Settings</h1>
        <p className="page-subtitle">Safety controls for the automation platform.</p>
      </div>

      {!profile ? (
        <div className="card p-6 text-sm text-slate-500">Create a profile first (see the Profile page) before configuring automation settings.</div>
      ) : (
        <div className="card p-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              {disabled ? <ShieldOff size={20} className="mt-0.5 text-red-600" /> : <ShieldCheck size={20} className="mt-0.5 text-emerald-600" />}
              <div>
                <p className="font-semibold text-slate-900">Autopilot Kill Switch</p>
                <p className="mt-1 max-w-lg text-sm text-slate-500">
                  Hard-stops every autopilot run for your account, checked fresh on every page of every
                  application — even one already in progress. Nothing gets auto-submitted while this is on,
                  regardless of any per-application autopilot setting.
                </p>
              </div>
            </div>
            <button
              className={disabled ? "btn-danger shrink-0" : "btn-ghost shrink-0"}
              disabled={saving}
              onClick={toggleKillSwitch}
            >
              {saving ? <Loader2 size={15} className="animate-spin" /> : null}
              {disabled ? "Disabled — Re-enable" : "Enabled — Disable Autopilot"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
