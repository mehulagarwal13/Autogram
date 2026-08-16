import { useEffect, useRef, useState } from "react";
import { CloudUpload, FileText, Star, Trash2, Loader2 } from "lucide-react";
import { api } from "../api";

const TYPES = [
  ["resume", "Resume"], ["cover_letter", "Cover Letter"], ["certificate", "Certificate"], ["other", "Other"],
];

export default function ResumeManagement({ toast }) {
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
        <h1 className="text-xl font-bold text-white">Resume Management</h1>
        <p className="text-sm text-slate-500">
          Upload every résumé variant, cover letter, or certificate you want automation to be able to pick from.
        </p>
      </div>

      <div className={`glass p-6 ${uploading ? "glass-active" : ""}`}>
        <div className="mb-4 grid gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="mb-1 block text-[11px] text-slate-500">Document type</span>
            <select className="input-dark" value={uploadType} onChange={(e) => setUploadType(e.target.value)}>
              {TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="block sm:col-span-2">
            <span className="mb-1 block text-[11px] text-slate-500">Job type tag (optional — e.g. "backend", "data-science")</span>
            <input className="input-dark" value={jobTypeTag} onChange={(e) => setJobTypeTag(e.target.value)} />
          </label>
        </div>
        <div
          onClick={() => !uploading && inputRef.current?.click()}
          className="flex cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed border-white/15 bg-white/[0.02] px-6 py-10 text-center transition hover:border-indigo-400/50 hover:bg-indigo-500/5"
        >
          {uploading ? <Loader2 size={28} className="mb-2 animate-spin text-indigo-300" /> : <CloudUpload size={28} className="mb-2 text-indigo-300" />}
          <p className="text-sm font-semibold text-slate-200">Click to upload a document</p>
          <p className="mt-1 text-xs text-slate-500">PDF or DOCX</p>
          <input ref={inputRef} type="file" accept=".pdf,.docx" className="hidden" onChange={(e) => onFile(e.target.files[0])} />
        </div>
      </div>

      <div className="glass overflow-hidden">
        <div className="border-b border-white/[0.06] px-6 py-4"><h2 className="font-semibold text-white">Your Documents</h2></div>
        {loading ? (
          <div className="flex justify-center py-10"><Loader2 size={22} className="animate-spin text-indigo-400" /></div>
        ) : documents.length === 0 ? (
          <p className="p-8 text-center text-sm text-slate-500">No documents uploaded yet.</p>
        ) : (
          <div className="divide-y divide-white/[0.05]">
            {documents.map((d) => (
              <div key={d.document_id} className="flex items-center justify-between px-6 py-3.5">
                <div className="flex items-center gap-3">
                  <FileText size={18} className="text-slate-500" />
                  <div>
                    <p className="text-sm font-medium text-slate-200">{d.original_filename}</p>
                    <p className="text-xs text-slate-500">
                      {TYPES.find(([v]) => v === d.document_type)?.[1] || d.document_type}
                      {d.job_type_tag ? ` · ${d.job_type_tag}` : ""}
                      {d.uploaded_at ? ` · ${new Date(d.uploaded_at).toLocaleDateString()}` : ""}
                    </p>
                  </div>
                  {d.is_default && <span className="chip bg-indigo-500/15 text-indigo-300">default</span>}
                </div>
                <div className="flex items-center gap-1.5">
                  {!d.is_default && (
                    <button className="btn-ghost !px-2.5 !py-1 text-xs" onClick={() => setDefault(d)} title="Make default">
                      <Star size={13} />
                    </button>
                  )}
                  <button className="btn-ghost !px-2.5 !py-1 text-xs !text-rose-300/80 hover:!border-rose-400/40" onClick={() => remove(d)}>
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
