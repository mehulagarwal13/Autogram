import { useEffect, useState } from "react";
import { Loader2, Save, Plus, Trash2, GraduationCap, Briefcase, Sparkles, UserCircle2 } from "lucide-react";
import { api } from "../api";

const FIELD_GROUPS = [
  {
    title: "Identity", fields: [
      ["full_name", "Full name"], ["preferred_name", "Preferred name"], ["email", "Email"], ["phone", "Phone"],
      ["linkedin_url", "LinkedIn"], ["github_url", "GitHub"], ["portfolio_url", "Portfolio"], ["website_url", "Website"],
    ],
  },
  {
    title: "Location", fields: [
      ["location", "Location (freeform)"], ["city", "City"], ["state", "State"], ["country", "Country"],
      ["postal_code", "Postal code"], ["time_zone", "Time zone"],
    ],
  },
  {
    title: "Professional", fields: [
      ["current_company", "Current company"], ["current_role", "Current role"],
      ["years_of_experience", "Years of experience", "number"], ["notice_period_days", "Notice period (days)", "number"],
      ["expected_salary", "Expected salary", "number"], ["expected_salary_currency", "Currency"],
      ["highest_education_level", "Highest education level"], ["referral_source", "How did you hear about roles?"],
    ],
  },
  {
    title: "Work Authorization & Compliance", fields: [
      ["work_authorization", "Work authorization (freeform)"], ["visa_status", "Visa status (freeform)"],
      ["visa_type", "Visa type"], ["remote_preference", "Remote preference"],
    ],
  },
];

const BOOL_FIELDS = [
  ["work_authorized", "Authorized to work now"], ["requires_sponsorship", "Will require sponsorship"],
  ["willing_to_relocate", "Willing to relocate"], ["willing_to_travel", "Willing to travel"],
  ["willing_background_check", "Willing to complete a background check"],
];

const SKILL_KEYS = ["programming_languages", "frameworks", "tools", "certifications", "technical_skills", "soft_skills"];

function SectionTitle({ icon: Icon, children }) {
  return (
    <h2 className="mb-4 flex items-center gap-2.5 font-semibold text-slate-900">
      <span className="flex h-7 w-7 items-center justify-center rounded-md bg-brand-50 text-brand-600">
        <Icon size={15} />
      </span>
      {children}
    </h2>
  );
}

export default function Profile({ toast }) {
  const [profile, setProfile] = useState(null);
  const [exists, setExists] = useState(true);
  const [education, setEducation] = useState([]);
  const [experience, setExperience] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [newEdu, setNewEdu] = useState({ degree: "", university: "", field_of_study: "", start_date: "", end_date: "" });
  const [newExp, setNewExp] = useState({ company_name: "", job_title: "", start_date: "", end_date: "", description: "" });
  const [skillsText, setSkillsText] = useState({});

  async function load() {
    try {
      const p = await api.getProfile();
      setProfile(p);
      setSkillsText(Object.fromEntries(SKILL_KEYS.map((k) => [k, (p.skills?.[k] || []).join(", ")])));
      const [edu, exp] = await Promise.all([api.listEducation(), api.listExperience()]);
      setEducation(edu);
      setExperience(exp);
    } catch {
      setExists(false);
      setProfile({});
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  function setField(key, value) {
    setProfile((p) => ({ ...p, [key]: value }));
  }

  async function save() {
    setSaving(true);
    try {
      if (exists) {
        const updated = await api.updateProfile(profile);
        setProfile(updated);
      } else {
        const created = await api.createProfile(profile);
        setProfile(created);
        setExists(true);
      }
      toast("Profile saved.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function saveSkills() {
    setSaving(true);
    try {
      const body = Object.fromEntries(
        SKILL_KEYS.map((k) => [k, skillsText[k].split(",").map((s) => s.trim()).filter(Boolean)])
      );
      const updated = await api.setSkills(body);
      setProfile(updated);
      toast("Skills saved.", "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setSaving(false);
    }
  }

  async function addEducation() {
    if (!newEdu.degree && !newEdu.university) return toast("Add at least a degree or university.", "error");
    try {
      const entry = await api.addEducation(newEdu);
      setEducation((e) => [entry, ...e]);
      setNewEdu({ degree: "", university: "", field_of_study: "", start_date: "", end_date: "" });
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function addExperience() {
    if (!newExp.company_name && !newExp.job_title) return toast("Add at least a company or job title.", "error");
    try {
      const entry = await api.addExperience(newExp);
      setExperience((e) => [entry, ...e]);
      setNewExp({ company_name: "", job_title: "", start_date: "", end_date: "", description: "" });
    } catch (e) {
      toast(e.message, "error");
    }
  }

  if (loading) return <div className="flex justify-center py-20"><Loader2 size={26} className="animate-spin text-brand-600" /></div>;

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <h1 className="page-title">Profile</h1>
        <p className="page-subtitle">
          The source of truth automation fills applications from. Nothing here is ever guessed — only what you enter or upload.
        </p>
      </div>

      {FIELD_GROUPS.map((group) => (
        <div key={group.title} className="card p-6">
          <SectionTitle icon={UserCircle2}>{group.title}</SectionTitle>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {group.fields.map(([key, label, type]) => (
              <label key={key} className="block">
                <span className="field-label">{label}</span>
                <input className="input" type={type || "text"} value={profile[key] ?? ""}
                  onChange={(e) => setField(key, type === "number" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value)} />
              </label>
            ))}
          </div>
        </div>
      ))}

      <div className="card p-6">
        <h2 className="mb-1 font-semibold text-slate-900">Yes/No Screening Facts</h2>
        <p className="mb-3 text-xs text-slate-500">Left blank ("never asked") until you set it — automation never guesses these.</p>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {BOOL_FIELDS.map(([key, label]) => (
            <label key={key} className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
              <span className="text-xs text-slate-700">{label}</span>
              <select className="input !w-28 !py-1 text-xs" value={profile[key] === null || profile[key] === undefined ? "" : String(profile[key])}
                onChange={(e) => setField(key, e.target.value === "" ? null : e.target.value === "true")}>
                <option value="">Never asked</option>
                <option value="true">Yes</option>
                <option value="false">No</option>
              </select>
            </label>
          ))}
        </div>
      </div>

      <button className="btn-primary" disabled={saving} onClick={save}>
        {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} Save Profile
      </button>

      {/* Education */}
      <div className="card p-6">
        <SectionTitle icon={GraduationCap}>Education</SectionTitle>
        <div className="mb-4 grid gap-2 sm:grid-cols-5">
          <input className="input" placeholder="Degree" value={newEdu.degree} onChange={(e) => setNewEdu({ ...newEdu, degree: e.target.value })} />
          <input className="input" placeholder="University" value={newEdu.university} onChange={(e) => setNewEdu({ ...newEdu, university: e.target.value })} />
          <input className="input" placeholder="Field of study" value={newEdu.field_of_study} onChange={(e) => setNewEdu({ ...newEdu, field_of_study: e.target.value })} />
          <input className="input" placeholder="Start (e.g. 2018)" value={newEdu.start_date} onChange={(e) => setNewEdu({ ...newEdu, start_date: e.target.value })} />
          <div className="flex gap-2">
            <input className="input" placeholder="End (e.g. 2022)" value={newEdu.end_date} onChange={(e) => setNewEdu({ ...newEdu, end_date: e.target.value })} />
            <button className="btn-primary !px-3" onClick={addEducation}><Plus size={16} /></button>
          </div>
        </div>
        <div className="space-y-2">
          {education.map((e) => (
            <div key={e.education_id} className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm">
              <span className="text-slate-700">{e.degree}{e.field_of_study ? `, ${e.field_of_study}` : ""} — {e.university} ({e.start_date}–{e.end_date || "present"})</span>
              <button className="text-slate-400 hover:text-red-600" onClick={async () => { await api.deleteEducation(e.education_id); setEducation((xs) => xs.filter((x) => x.education_id !== e.education_id)); }}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          {education.length === 0 && <p className="text-xs text-slate-400">No education entries yet.</p>}
        </div>
      </div>

      {/* Experience */}
      <div className="card p-6">
        <SectionTitle icon={Briefcase}>Work Experience</SectionTitle>
        <div className="mb-4 grid gap-2 sm:grid-cols-5">
          <input className="input" placeholder="Company" value={newExp.company_name} onChange={(e) => setNewExp({ ...newExp, company_name: e.target.value })} />
          <input className="input" placeholder="Job title" value={newExp.job_title} onChange={(e) => setNewExp({ ...newExp, job_title: e.target.value })} />
          <input className="input" placeholder="Start (e.g. 2021-03)" value={newExp.start_date} onChange={(e) => setNewExp({ ...newExp, start_date: e.target.value })} />
          <input className="input" placeholder="End (blank = current)" value={newExp.end_date} onChange={(e) => setNewExp({ ...newExp, end_date: e.target.value })} />
          <button className="btn-primary" onClick={addExperience}><Plus size={16} /> Add</button>
        </div>
        <div className="space-y-2">
          {experience.map((e) => (
            <div key={e.experience_id} className="flex items-center justify-between rounded-lg border border-slate-200 bg-white px-4 py-2.5 text-sm">
              <span className="text-slate-700">{e.job_title} at {e.company_name} ({e.start_date}–{e.end_date || "present"})</span>
              <button className="text-slate-400 hover:text-red-600" onClick={async () => { await api.deleteExperience(e.experience_id); setExperience((xs) => xs.filter((x) => x.experience_id !== e.experience_id)); }}>
                <Trash2 size={14} />
              </button>
            </div>
          ))}
          {experience.length === 0 && <p className="text-xs text-slate-400">No experience entries yet.</p>}
        </div>
      </div>

      {/* Skills */}
      <div className="card p-6">
        <SectionTitle icon={Sparkles}>Skills</SectionTitle>
        <div className="grid gap-3 sm:grid-cols-2">
          {SKILL_KEYS.map((k) => (
            <label key={k} className="block">
              <span className="field-label capitalize">{k.replaceAll("_", " ")} (comma-separated)</span>
              <input className="input" value={skillsText[k] || ""} onChange={(e) => setSkillsText({ ...skillsText, [k]: e.target.value })} />
            </label>
          ))}
        </div>
        <button className="btn-primary mt-4" disabled={saving} onClick={saveSkills}>
          {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />} Save Skills
        </button>
      </div>
    </div>
  );
}
