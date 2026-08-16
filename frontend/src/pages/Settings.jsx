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

  if (loading) return <div className="flex justify-center py-20"><Loader2 size={28} className="animate-spin text-indigo-400" /></div>;

  const disabled = !!profile?.autopilot_globally_disabled;

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="text-xl font-bold text-white">Settings</h1>
        <p className="text-sm text-slate-500">Safety controls for the automation platform.</p>
      </div>

      {!profile ? (
        <div className="glass p-6 text-sm text-slate-500">Create a profile first (see the Profile page) before configuring automation settings.</div>
      ) : (
        <div className="glass p-6">
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              {disabled ? <ShieldOff size={22} className="mt-0.5 text-rose-400" /> : <ShieldCheck size={22} className="mt-0.5 text-emerald-400" />}
              <div>
                <p className="font-semibold text-white">Autopilot Kill Switch</p>
                <p className="mt-1 max-w-lg text-sm text-slate-400">
                  Hard-stops every autopilot run for your account, checked fresh on every page of every
                  application — even one already in progress. Nothing gets auto-submitted while this is on,
                  regardless of any per-application autopilot setting.
                </p>
              </div>
            </div>
            <button
              className={disabled ? "btn-primary !from-rose-500 !to-rose-600 shrink-0" : "btn-ghost shrink-0"}
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
