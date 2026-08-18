import { useRef, useState } from "react";
import { CloudUpload, FileText, Sparkles, Cpu, CheckCircle2, Loader2, AlertCircle } from "lucide-react";
import { api } from "../api";

const STEPS = [
  { key: "upload", label: "Upload", icon: CloudUpload },
  { key: "extract", label: "Extract", icon: FileText },
  { key: "parse", label: "Parse (AI)", icon: Sparkles },
  { key: "embed", label: "Embed", icon: Cpu },
];

function StepDot({ state, Icon }) {
  if (state === "done") return <CheckCircle2 size={20} className="animate-bounce-in text-emerald-600" />;
  if (state === "running") return <Loader2 size={20} className="animate-spin text-brand-600" />;
  if (state === "error") return <AlertCircle size={20} className="text-red-600" />;
  return <Icon size={20} className="text-slate-300" />;
}

export default function UploadPanel({ resume, setResume, toast }) {
  const inputRef = useRef(null);
  const [dragOver, setDragOver] = useState(false);
  const [steps, setSteps] = useState({});
  const [busy, setBusy] = useState(false);
  const [parsedPreview, setParsedPreview] = useState(null);

  const setStep = (key, state) => setSteps((s) => ({ ...s, [key]: state }));

  async function runPipeline(file) {
    setBusy(true);
    setParsedPreview(null);
    setSteps({ upload: "running" });
    try {
      const up = await api.uploadResume(file);
      setResume({ id: up.resume_id, filename: up.original_filename });
      setStep("upload", "done");
      if (up.status === "duplicate_of_existing") toast("Same file uploaded before — reusing it.", "info");

      setStep("extract", "running");
      await api.extract(up.resume_id);
      setStep("extract", "done");

      setStep("parse", "running");
      const parsed = await api.parse(up.resume_id);
      setParsedPreview(parsed);
      setStep("parse", "done");

      setStep("embed", "running");
      await api.embed(up.resume_id);
      setStep("embed", "done");

      toast("Resume fully processed — ready to match!", "success");
    } catch (e) {
      setSteps((s) => {
        const running = Object.keys(s).find((k) => s[k] === "running");
        return running ? { ...s, [running]: "error" } : s;
      });
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  }

  function onFile(file) {
    if (!file) return;
    if (!/\.(pdf|docx)$/i.test(file.name)) {
      toast("Only PDF and DOCX files are supported.", "error");
      return;
    }
    runPipeline(file);
  }

  const p = parsedPreview?.parsed_resume;

  return (
    <div className={`card animate-fade-up p-6 ${busy ? "card-active" : ""}`}>
      <h2 className="text-base font-semibold text-slate-900">Your Resume</h2>
      <p className="mt-0.5 mb-5 text-sm text-slate-500">
        Drop a resume and the agent extracts, parses, and embeds it automatically.
      </p>

      {/* Dropzone */}
      <div
        onClick={() => !busy && inputRef.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); onFile(e.dataTransfer.files[0]); }}
        className={`flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-10 text-center transition-colors
          ${dragOver ? "border-brand-400 bg-brand-50" : "border-slate-200 bg-slate-50 hover:border-brand-300 hover:bg-brand-50/40"}`}
      >
        <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-full bg-brand-50">
          <CloudUpload size={22} className="text-brand-600" />
        </div>
        <p className="text-sm font-medium text-slate-800">
          {resume ? resume.filename : "Drag & drop your resume"}
        </p>
        <p className="mt-1 text-xs text-slate-500">PDF or DOCX · max 5 MB</p>
        <input ref={inputRef} type="file" accept=".pdf,.docx" className="hidden"
          onChange={(e) => onFile(e.target.files[0])} />
      </div>

      {/* Pipeline stepper */}
      <div className="mt-6 flex items-center justify-between">
        {STEPS.map((s, i) => (
          <div key={s.key} className="flex flex-1 items-center">
            <div className="flex flex-col items-center gap-1.5">
              <StepDot state={steps[s.key]} Icon={s.icon} />
              <span className={`text-[11px] font-medium ${steps[s.key] === "done" ? "text-emerald-600" : steps[s.key] ? "text-slate-700" : "text-slate-400"}`}>
                {s.label}
              </span>
            </div>
            {i < STEPS.length - 1 && (
              <div className={`mx-2 h-px flex-1 ${steps[STEPS[i + 1].key] ? "bg-brand-300" : "bg-slate-200"}`} />
            )}
          </div>
        ))}
      </div>

      {/* Parsed preview */}
      {p && (
        <div className="mt-6 animate-fade-up rounded-lg border border-slate-200 bg-slate-50 p-4">
          <div className="flex items-center justify-between">
            <p className="font-medium text-slate-900">{p.full_name || "Unnamed candidate"}</p>
            <span className="badge badge-brand">
              confidence {Math.round((parsedPreview.confidence_score || 0) * 100)}%
            </span>
          </div>
          <p className="mt-0.5 text-xs text-slate-500">
            {[p.email, p.location, p.total_years_experience && `${p.total_years_experience} yrs exp`]
              .filter(Boolean).join(" · ")}
          </p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            {(p.skills || []).slice(0, 12).map((s) => (
              <span key={s} className="badge badge-neutral">{s}</span>
            ))}
            {(p.skills || []).length > 12 && (
              <span className="badge badge-neutral">+{p.skills.length - 12} more</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
