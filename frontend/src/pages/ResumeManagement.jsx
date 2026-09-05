import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { CloudUpload, FileText, Star, Trash2, Loader2 } from "lucide-react";
import { api } from "../api";

const TYPES = [
  ["resume", "Resume"], ["cover_letter", "Cover Letter"], ["certificate", "Certificate"], ["other", "Other"],
];

export default function ResumeManagement({ toast }) {
  const navigate = useNavigate();
  const [drafting, setDrafting] = useState(null);

  async function buildProfile(document) {
    setDrafting(document.document_id);
    try {
      const draft = await api.getProfileDraft(document.document_id);
      navigate("/profile", { state: { resumeDraft: draft } });
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setDrafting(null);
    }
  }
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [uploadType, setUploadType] = useState("resume");
  const [jobTypeTag, setJobTypeTag] = useState("");
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef(null);

  async function load() {
    try {
      setDocuments(await api.listDocuments());
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function onFile(file) {
    if (!file) return;
    setUploading(true);
    try {
      const doc = await api.uploadDocument(file, { documentType: uploadType, jobTypeTag: jobTypeTag || undefined });
      setDocuments((docs) => [doc, ...docs.filter((d) => d.document_id !== doc.document_id)]);
      toast(`${doc.original_filename} uploaded.`, "success");
    } catch (e) {
      toast(e.message, "error");
    } finally {
      setUploading(false);
    }
  }

  async function setDefault(doc) {
    try {
      await api.setDefaultDocument(doc.document_id);
      await load();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function remove(doc) {
    if (!window.confirm(`Delete "${doc.original_filename}"?`)) return;
    try {
      await api.deleteDocument(doc.document_id);
      setDocuments((docs) => docs.filter((d) => d.document_id !== doc.document_id));
    } catch (e) {
      toast(e.message, "error");
    }
  }

  return (
    <div className="animate-fade-up space-y-5">
      <div>
        <p className="page-kicker">Ready for your next role</p>
        <h1 className="page-title">Your document library</h1>
        <p className="page-subtitle">
          Upload every résumé variant, cover letter, or certificate you want automation to be able to pick from.
        </p>
      </div>

      <div className={`card p-6 ${uploading ? "card-active" : ""}`}>
        <div className="mb-4 grid gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="field-label">Document type</span>
            <select className="input" value={uploadType} onChange={(e) => setUploadType(e.target.value)}>
              {TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="block sm:col-span-2">
            <span className="field-label">Job type tag (optional — e.g. "backend", "data-science")</span>
            <input className="input" value={jobTypeTag} onChange={(e) => setJobTypeTag(e.target.value)} />
          </label>
        </div>
        <div
          role="button"
          tabIndex={uploading ? -1 : 0}
          aria-label="Upload a document"
          aria-disabled={uploading}
          onKeyDown={(event) => { if (!uploading && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); inputRef.current?.click(); } }}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => { event.preventDefault(); if (!uploading) onFile(event.dataTransfer.files[0]); }}
          onClick={() => !uploading && inputRef.current?.click()}
          className="flex cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed border-slate-200 bg-slate-50 px-6 py-10 text-center transition-colors hover:border-brand-300 hover:bg-brand-50/40"
        >
          {uploading ? <Loader2 size={26} className="mb-2 animate-spin text-brand-600" /> : <CloudUpload size={26} className="mb-2 text-brand-600" />}
          <p className="text-sm font-medium text-slate-800">{uploading ? "Uploading your document…" : "Drop your document here, or click to browse"}</p>
          <p className="mt-1 text-xs text-slate-500">PDF or DOCX</p>
          <input ref={inputRef} type="file" accept=".pdf,.docx" className="hidden" onChange={(e) => { onFile(e.target.files[0]); e.target.value = ""; }} />
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="card-header"><h2 className="font-semibold text-slate-900">Your Documents</h2></div>
        {loading ? (
          <div className="flex justify-center py-10"><Loader2 size={20} className="animate-spin text-brand-600" /></div>
        ) : documents.length === 0 ? (
          <p className="p-8 text-center text-sm text-slate-500">No documents uploaded yet.</p>
        ) : (
          <div className="divide-y divide-slate-100">
            {documents.map((d) => (
              <div key={d.document_id} className="flex flex-wrap items-center justify-between gap-4 px-6 py-4">
                <div className="flex min-w-0 items-center gap-3">
                  <FileText size={18} className="text-slate-400" />
                  <div>
                    <p className="break-all text-sm font-medium text-slate-800">{d.original_filename}</p>
                    <p className="text-xs text-slate-500">
                      {TYPES.find(([v]) => v === d.document_type)?.[1] || d.document_type}
                      {d.job_type_tag ? ` · ${d.job_type_tag}` : ""}
                      {d.uploaded_at ? ` · ${new Date(d.uploaded_at).toLocaleDateString()}` : ""}
                    </p>
                  </div>
                  {d.is_default && <span className="badge badge-brand">default</span>}
                </div>
                <div className="flex items-center gap-1.5">
                  {d.document_type === "resume" && <button className="btn-ghost text-xs" disabled={Boolean(drafting)} onClick={() => buildProfile(d)}>
                    {drafting === d.document_id ? <Loader2 size={14} className="animate-spin" /> : null} Build profile
                  </button>}
                  {!d.is_default && (
                    <button className="btn-ghost !px-2.5 !py-1 text-xs" onClick={() => setDefault(d)} title="Make default">
                      <Star size={13} />
                    </button>
                  )}
                  <button aria-label={`Delete ${d.original_filename}`} className="btn-ghost !px-2.5 !py-1 text-xs !text-red-700" onClick={() => remove(d)}>
                    <Trash2 size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
