import { useEffect, useState } from "react";
import { ShieldOff, ShieldCheck, Loader2, Trash2, Clock, Trash } from "lucide-react";
import { api } from "../api";

const TRUST_LEVELS = [
  { value: "FULL_MANUAL_REVIEW", label: "Full manual review", hint: "Always stop before submit." },
  { value: "TRUSTED_AUTO_SUBMIT", label: "Trusted (auto-submit)", hint: "Submit automatically once confidence is high." },
  { value: "DRAFT_ONLY", label: "Draft only", hint: "Fill and leave open — never submit." },
];

const RETENTION_FIELDS = [
  { key: "screenshot_retention_days", label: "Screenshots", hint: "Field-fill screenshots and vision-fallback crops." },
  { key: "run_history_retention_days", label: "Run history", hint: "Step-by-step logs, traces, and per-run detail." },
  { key: "hitl_request_retention_days", label: "Verification requests", hint: "Concluded OTP/CAPTCHA/login pauses. Never a code itself — those are never stored." },
];

export default function Settings({ toast }) {
  const [profile, setProfile] = useState(null);
  const [trustLevels, setTrustLevels] = useState([]);
  const [retentionPolicy, setRetentionPolicy] = useState(null);
  const [retentionDraft, setRetentionDraft] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingDomain, setSavingDomain] = useState(null);
  const [savingRetention, setSavingRetention] = useState(false);
  const [purging, setPurging] = useState(false);

  useEffect(() => {
    Promise.all([
      api.getProfile().catch(() => null),
      api.listSiteTrustLevels().catch(() => []),
      api.getRetentionPolicy().catch(() => null),
    ]).then(([p, levels, policy]) => {
      setProfile(p);
      setTrustLevels(levels);
      setRetentionPolicy(policy);
      setRetentionDraft(policy);
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

  async function changeDefaultTrustLevel(level) {
    if (!profile) return;
    setSaving(true);
    try {
      const updated = await api.updateAutomationSettings({
        autopilot_globally_disabled: profile.autopilot_globally_disabled,
        default_trust_level: level,
      });
      setProfile((p) => ({ ...p, ...updated }));
      toast("Default trust level updated.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function changeSiteTrustLevel(domain, level) {
    setSavingDomain(domain);
    try {
      const updated = await api.setSiteTrustLevel(domain, level);
      setTrustLevels((rows) => rows.map((r) => (r.domain === domain ? updated : r)));
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSavingDomain(null);
    }
  }

  async function removeSiteTrustLevel(domain) {
    setSavingDomain(domain);
    try {
      await api.deleteSiteTrustLevel(domain);
      setTrustLevels((rows) => rows.filter((r) => r.domain !== domain));
      toast(`${domain} now uses your default trust level.`, "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSavingDomain(null);
    }
  }

  const retentionDirty = retentionPolicy && retentionDraft && RETENTION_FIELDS.some(
    (f) => Number(retentionDraft[f.key]) !== retentionPolicy[f.key],
  );

  async function saveRetentionPolicy() {
    setSavingRetention(true);
    try {
      const body = Object.fromEntries(RETENTION_FIELDS.map((f) => [f.key, Number(retentionDraft[f.key])]));
      const updated = await api.updateRetentionPolicy(body);
      setRetentionPolicy(updated);
      setRetentionDraft(updated);
      toast("Retention windows updated.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSavingRetention(false);
    }
  }

  async function purgeNow() {
    if (!window.confirm("Purge everything past your retention windows right now? This cannot be undone.")) return;
    setPurging(true);
    try {
      const { results } = await api.purgeRetentionNow();
      const total = results.reduce((sum, r) => sum + r.records_purged, 0);
      toast(total > 0 ? `Purged ${total} record(s).` : "Nothing was past its retention window.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setPurging(false);
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
          <div className={`card p-6 ${disabled ? "border-red-200" : ""}`}>
            <div className="flex flex-wrap items-start justify-between gap-4">
              <div className="flex items-start gap-3">
                <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg ${disabled ? "bg-red-50" : "bg-emerald-50"}`}>
                  {disabled ? <ShieldOff size={18} className="text-red-600" /> : <ShieldCheck size={18} className="text-emerald-600" />}
                </div>
                <div>
                  <p className="font-semibold text-slate-900">Autopilot Kill Switch</p>
                  <p className="mt-1 max-w-lg text-sm text-slate-500">
                    Hard-stops every autopilot run for your account, checked fresh on every page of every
                    application — even one already in progress. Nothing gets auto-submitted while this is on,
                    regardless of any per-application autopilot setting.
                  </p>
                  <p className={`mt-2 text-xs font-medium ${disabled ? "text-red-600" : "text-emerald-600"}`}>
                    {disabled ? "Autopilot is disabled account-wide" : "Autopilot is enabled"}
                  </p>
                </div>
              </div>

              <button
                role="switch"
                aria-checked={!disabled}
                disabled={saving}
                onClick={toggleKillSwitch}
                className={`relative inline-flex h-7 w-12 shrink-0 items-center rounded-full transition-colors duration-200 disabled:cursor-not-allowed disabled:opacity-60 ${
                  disabled ? "bg-slate-200" : "bg-emerald-500"
                }`}
              >
                <span
                  className={`inline-flex h-5 w-5 transform items-center justify-center rounded-full bg-white shadow-sm transition-transform duration-200 ${
                    disabled ? "translate-x-1" : "translate-x-6"
                  }`}
                >
                  {saving && <Loader2 size={11} className="animate-spin text-slate-400" />}
                </span>
              </button>
            </div>
          </div>

          <div className="card p-6">
            <p className="font-semibold text-slate-900">Trust Levels</p>
            <p className="mt-1 max-w-lg text-sm text-slate-500">
              How much Autogram is allowed to do on its own, per job site. A CAPTCHA or verification code always
              pauses for you regardless of this setting — it isn't something automation can bypass.
            </p>

            <div className="mt-4">
              <p className="eyebrow">Default for new sites</p>
              <div className="mt-1.5 flex flex-wrap gap-2">
                {TRUST_LEVELS.map((level) => (
                  <button
                    key={level.value}
                    disabled={saving}
                    onClick={() => changeDefaultTrustLevel(level.value)}
                    title={level.hint}
                    className={`rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50 ${
                      profile.default_trust_level === level.value
                        ? "border-brand-600 bg-brand-50 text-brand-700"
                        : "border-slate-200 text-slate-600 hover:border-slate-300"
                    }`}
                  >
                    {level.label}
                  </button>
                ))}
              </div>
              <p className="mt-1.5 text-xs text-slate-400">
                A brand-new site you've never automated before always starts at Full manual review, regardless of
                this default — this only applies once Autogram has actually seen that site's domain.
              </p>
            </div>

            {trustLevels.length > 0 && (
              <div className="mt-5 border-t border-slate-100 pt-4">
                <p className="eyebrow">Per-site overrides</p>
                <div className="mt-2 divide-y divide-slate-100">
                  {trustLevels.map((row) => (
                    <div key={row.domain} className="flex flex-wrap items-center justify-between gap-2 py-2">
                      <span className="font-mono text-xs text-slate-700">{row.domain}</span>
                      <div className="flex items-center gap-1.5">
                        <select
                          value={row.trust_level}
                          disabled={savingDomain === row.domain}
                          onChange={(e) => changeSiteTrustLevel(row.domain, e.target.value)}
                          className="input !w-auto !py-1.5 text-xs"
                        >
                          {TRUST_LEVELS.map((level) => (
                            <option key={level.value} value={level.value}>{level.label}</option>
                          ))}
                        </select>
                        <button
                          onClick={() => removeSiteTrustLevel(row.domain)}
                          disabled={savingDomain === row.domain}
                          title="Remove override — revert to the default"
                          className="btn-ghost !px-2 !py-1.5 text-xs text-red-600 hover:bg-red-50"
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {retentionDraft && (
            <div className="card p-6">
              <div className="flex items-start gap-3">
                <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-slate-50">
                  <Clock size={18} className="text-slate-500" />
                </div>
                <div>
                  <p className="font-semibold text-slate-900">Data Retention</p>
                  <p className="mt-1 max-w-lg text-sm text-slate-500">
                    How long Autogram keeps screenshots, run history, and verification-request records after an
                    application or task finishes. Checked nightly — nothing still in progress is ever touched.
                  </p>
                </div>
              </div>

              <div className="mt-4 grid gap-3 sm:grid-cols-3">
                {RETENTION_FIELDS.map((field) => (
                  <div key={field.key}>
                    <label className="eyebrow" htmlFor={field.key}>{field.label}</label>
                    <div className="mt-1 flex items-center gap-1.5">
                      <input
                        id={field.key}
                        type="number"
                        min={1}
                        value={retentionDraft[field.key]}
                        onChange={(e) => setRetentionDraft((d) => ({ ...d, [field.key]: e.target.value }))}
                        className="input !w-20"
                      />
                      <span className="text-xs text-slate-400">days</span>
                    </div>
                    <p className="mt-1 text-xs text-slate-400">{field.hint}</p>
                  </div>
                ))}
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-4">
                <button
                  className="btn-primary !px-3 !py-1.5 text-xs"
                  disabled={!retentionDirty || savingRetention}
                  onClick={saveRetentionPolicy}
                >
                  {savingRetention ? <Loader2 size={13} className="animate-spin" /> : "Save windows"}
                </button>
                <button
                  className="btn-ghost !px-3 !py-1.5 text-xs text-red-600 hover:bg-red-50"
                  disabled={purging}
                  onClick={purgeNow}
                >
                  <Trash size={13} /> {purging ? "Purging…" : "Purge now"}
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
