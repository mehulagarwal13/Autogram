import { useEffect, useState } from "react";
import { ShieldOff, ShieldCheck, Loader2, Globe, Trash, Plus } from "lucide-react";
import { api } from "../api";

const TRUST_LEVELS = [
  { value: "FULL_MANUAL_REVIEW", label: "Full manual review", hint: "Every application waits for you to click submit." },
  { value: "TRUSTED_AUTO_SUBMIT", label: "Trusted auto-submit", hint: "High-confidence fills on public application pages submit without waiting." },
  { value: "DRAFT_ONLY", label: "Draft only", hint: "Behaves like full manual review today — reserved for a future fill-and-stop mode." },
];

function trustLabel(value) {
  return TRUST_LEVELS.find((t) => t.value === value)?.label ?? value;
}

export default function Settings({ toast }) {
  const [profile, setProfile] = useState(null);
  const [siteTrustLevels, setSiteTrustLevels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingTrust, setSavingTrust] = useState(false);
  const [newDomain, setNewDomain] = useState("");
  const [newTrustLevel, setNewTrustLevel] = useState("TRUSTED_AUTO_SUBMIT");
  const [addingOverride, setAddingOverride] = useState(false);

  useEffect(() => {
    Promise.all([
      api.getProfile().catch(() => null),
      api.listSiteTrustLevels().catch(() => []),
    ]).then(([p, levels]) => {
      setProfile(p);
      setSiteTrustLevels(levels);
    }).finally(() => setLoading(false));
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

  async function changeDefaultTrustLevel(value) {
    if (!profile || value === profile.default_trust_level) return;
    setSavingTrust(true);
    try {
      const updated = await api.updateAutomationSettings({
        autopilot_globally_disabled: profile.autopilot_globally_disabled,
        default_trust_level: value,
      });
      setProfile((p) => ({ ...p, ...updated }));
      toast(`Default trust level set to "${trustLabel(value)}".`, "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSavingTrust(false);
    }
  }

  async function addOverride(e) {
    e.preventDefault();
    const domain = newDomain.trim().toLowerCase();
    if (!domain) return;
    setAddingOverride(true);
    try {
      const row = await api.setSiteTrustLevel(domain, newTrustLevel);
      setSiteTrustLevels((rows) => [...rows.filter((r) => r.domain !== row.domain), row]);
      setNewDomain("");
      toast(`Trust level for ${domain} set to "${trustLabel(row.trust_level)}".`, "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setAddingOverride(false);
    }
  }

  async function removeOverride(domain) {
    try {
      await api.deleteSiteTrustLevel(domain);
      setSiteTrustLevels((rows) => rows.filter((r) => r.domain !== domain));
      toast(`Removed override for ${domain} — it now uses your default.`, "success");
    } catch (e) {
      toast(e.message, "error");
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
        <>
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

          <div className="card p-6">
            <div className="flex items-start gap-3">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-50">
                <Globe size={18} className="text-slate-500" />
              </div>
              <div>
                <p className="font-semibold text-slate-900">Trust Levels</p>
                <p className="mt-1 max-w-lg text-sm text-slate-500">
                  Controls whether a high-confidence, fully-filled application can submit itself, or always
                  waits for you. Trust is an additional requirement, never a shortcut — a trusted site with a
                  low-confidence fill still lands in review.
                </p>
              </div>
            </div>

            <div className="mt-4 border-t border-slate-100 pt-4">
              <label className="eyebrow" htmlFor="default-trust-level">Default for new sites</label>
              <select
                id="default-trust-level"
                className="input mt-1 max-w-xs"
                value={profile.default_trust_level}
                disabled={savingTrust}
                onChange={(e) => changeDefaultTrustLevel(e.target.value)}
              >
                {TRUST_LEVELS.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
              <p className="mt-1 text-xs text-slate-400">
                {TRUST_LEVELS.find((t) => t.value === profile.default_trust_level)?.hint}
              </p>
            </div>

            <div className="mt-4 border-t border-slate-100 pt-4">
              <p className="eyebrow">Per-site overrides</p>
              {siteTrustLevels.length === 0 ? (
                <p className="mt-1 text-sm text-slate-400">No overrides yet — every domain uses the default above.</p>
              ) : (
                <ul className="mt-2 divide-y divide-slate-100">
                  {siteTrustLevels.map((row) => (
                    <li key={row.domain} className="flex items-center justify-between gap-3 py-2">
                      <div>
                        <p className="text-sm font-medium text-slate-800">{row.domain}</p>
                        <p className="text-xs text-slate-400">{trustLabel(row.trust_level)}</p>
                      </div>
                      <button
                        className="btn-ghost !px-2 !py-1 text-xs text-red-600 hover:bg-red-50"
                        onClick={() => removeOverride(row.domain)}
                      >
                        <Trash size={13} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}

              <form className="mt-3 flex flex-wrap items-center gap-2" onSubmit={addOverride}>
                <input
                  type="text"
                  placeholder="boards.greenhouse.io"
                  className="input !w-56"
                  value={newDomain}
                  onChange={(e) => setNewDomain(e.target.value)}
                />
                <select
                  className="input !w-auto"
                  value={newTrustLevel}
                  onChange={(e) => setNewTrustLevel(e.target.value)}
                >
                  {TRUST_LEVELS.map((t) => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                  ))}
                </select>
                <button className="btn-primary !px-3 !py-1.5 text-xs" disabled={addingOverride || !newDomain.trim()} type="submit">
                  {addingOverride ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}
                  Add override
                </button>
              </form>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
