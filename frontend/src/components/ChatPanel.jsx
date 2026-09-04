import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bot, CheckCircle2, Loader2, MessageSquare, Paperclip, Send,
  ShieldAlert, User as UserIcon, Wifi, WifiOff, X,
} from "lucide-react";
import { api, openChatStream } from "../api";

const ATTACHMENT_TYPES = [
  ["resume", "Resume / CV"],
  ["cover_letter", "Cover letter"],
  ["certificate", "Certificate"],
  ["other", "Other document"],
];

/**
 * Durable conversation surface for one automation attempt.
 *
 * HTTP is the transcript authority. The socket only triggers a refetch, so a
 * dropped event can make the UI briefly stale but never permanently wrong.
 * Inputs are request-aware: text is available only for an answer request and
 * attachments only for a file-upload request.
 */
export default function ChatPanel({
  scope,
  resourceId,
  activeRequest,
  onRespond,
  onAttach,
  documents = [],
  busy,
}) {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [draft, setDraft] = useState("");
  const [attachmentType, setAttachmentType] = useState("resume");
  const [attachmentError, setAttachmentError] = useState("");
  const [uploading, setUploading] = useState(false);
  const bottomRef = useRef(null);
  const pollRef = useRef(null);
  const fileRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      setMessages(await api.getChatTranscript(scope, resourceId));
    } catch {
      // Transient: the next event or poll tick retries.
    } finally {
      setLoading(false);
    }
  }, [scope, resourceId]);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => {
    const socket = openChatStream(scope, resourceId, () => refresh(), {
      onError: () => setLive(false),
    });
    if (socket) {
      socket.onopen = () => setLive(true);
      socket.onclose = () => setLive(false);
    }
    pollRef.current = setInterval(refresh, 15000);
    return () => {
      clearInterval(pollRef.current);
      socket?.close();
    };
  }, [scope, resourceId, refresh]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  function submitDraft(event) {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    onRespond?.(text);
    setDraft("");
  }

  async function attachFile(file) {
    if (!file || !onAttach || uploading || busy) return;
    if (!/\.(pdf|docx|png|jpe?g|txt)$/i.test(file.name)) {
      setAttachmentError("Choose a PDF, DOCX, PNG, JPG, or TXT file.");
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      setAttachmentError("This file is larger than the 10 MB limit.");
      return;
    }
    setAttachmentError("");
    setUploading(true);
    try {
      await onAttach(file, attachmentType);
      if (fileRef.current) fileRef.current.value = "";
    } catch (error) {
      setAttachmentError(error?.message || "The document could not be attached.");
    } finally {
      setUploading(false);
    }
  }

  const acceptsFreeText = activeRequest?.request_type === "ANSWER_REQUIRED";
  const acceptsAttachment = activeRequest?.request_type === "FILE_UPLOAD_REQUIRED";

  return (
    <section className="card flex min-h-[560px] flex-col overflow-hidden" aria-label="Agent conversation">
      <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <MessageSquare size={16} className="text-brand-600" /> Agent conversation
          </h2>
          <p className="mt-0.5 text-[11px] text-slate-500">Decisions, requests, and your replies in one durable record.</p>
        </div>
        <span
          className={`flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[10px] font-semibold ${
            live ? "bg-emerald-50 text-emerald-700" : "bg-slate-100 text-slate-500"
          }`}
          title={live ? "Live updates connected" : "Live updates unavailable — refreshing periodically instead"}
        >
          {live ? <Wifi size={11} /> : <WifiOff size={11} />}
          {live ? "Live" : "Polling"}
        </span>
      </div>

      {documents.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 bg-slate-50/70 px-5 py-2.5">
          <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">Agent has</span>
          {documents.map((document, index) => (
            <span
              key={document.document_id || `${document.original_filename}-${index}`}
              className="inline-flex max-w-[230px] items-center gap-1.5 rounded-md border border-emerald-200 bg-white px-2 py-1 text-[11px] font-medium text-emerald-700"
              title={`${document.original_filename} is available for this task`}
            >
              <CheckCircle2 size={11} />
              <span className="truncate">{document.original_filename}</span>
            </span>
          ))}
        </div>
      )}

      <div className="flex-1 space-y-4 overflow-y-auto bg-gradient-to-b from-white to-slate-50/40 p-5">
        {loading && (
          <p className="flex items-center gap-2 text-xs text-slate-500">
            <Loader2 size={13} className="animate-spin" /> Loading conversation...
          </p>
        )}
        {!loading && messages.length === 0 && (
          <div className="flex min-h-48 flex-col items-center justify-center text-center">
            <span className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl bg-brand-50 text-brand-600"><Bot size={18} /></span>
            <p className="text-sm font-medium text-slate-700">The conversation is just getting started</p>
            <p className="mt-1 max-w-sm text-xs leading-relaxed text-slate-500">
              Nothing yet — messages appear here as the automation runs and whenever it needs you.
            </p>
          </div>
        )}
        {messages.map((message) => <Message key={message.message_id} message={message} />)}
        <div ref={bottomRef} />
      </div>

      {acceptsFreeText && (
        <form onSubmit={submitDraft} className="border-t border-slate-200 bg-white p-4">
          <div className="mb-3 flex items-start gap-2 rounded-lg bg-amber-50 px-3 py-2.5 text-xs text-amber-900">
            <ShieldAlert size={14} className="mt-0.5 shrink-0" />
            <span>{activeRequest.message}</span>
          </div>
          <div className="flex gap-2">
            <input
              className="input flex-1"
              placeholder="Type your answer..."
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              disabled={busy}
              autoFocus
            />
            <button type="submit" className="btn-primary" disabled={busy || !draft.trim()} aria-label="Send answer">
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
              <span className="hidden sm:inline">Send</span>
            </button>
          </div>
        </form>
      )}

      {acceptsAttachment && (
        <div className="border-t border-slate-200 bg-white p-4">
          <div className="mb-3 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
            <Paperclip size={14} className="mt-0.5 shrink-0 text-amber-700" />
            <div>
              <p className="text-xs font-semibold text-amber-950">A document is required to continue</p>
              <p className="mt-0.5 text-[11px] leading-relaxed text-amber-800">{activeRequest.message}</p>
            </div>
          </div>
          <div className="grid gap-2 sm:grid-cols-[170px_minmax(0,1fr)]">
            <select
              className="input"
              value={attachmentType}
              onChange={(event) => { setAttachmentType(event.target.value); setAttachmentError(""); }}
              disabled={busy || uploading}
              aria-label="Document type"
            >
              {ATTACHMENT_TYPES.map(([value, label]) => <option value={value} key={value}>{label}</option>)}
            </select>
            <button
              type="button"
              className="btn-primary w-full"
              onClick={() => fileRef.current?.click()}
              disabled={busy || uploading}
            >
              {uploading ? <Loader2 size={15} className="animate-spin" /> : <Paperclip size={15} />}
              {uploading ? "Attaching securely..." : "Choose file & continue"}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.png,.jpg,.jpeg,.txt"
              className="hidden"
              onChange={(event) => attachFile(event.target.files?.[0])}
            />
          </div>
          <div className="mt-2 flex items-center justify-between gap-3 text-[10px] text-slate-500">
            <span>PDF, DOCX, PNG, JPG, or TXT · 10 MB max · scoped to this task</span>
            {attachmentError && (
              <button type="button" onClick={() => setAttachmentError("")} className="flex items-center gap-1 text-red-600">
                {attachmentError} <X size={10} />
              </button>
            )}
          </div>
        </div>
      )}

      {!acceptsFreeText && !acceptsAttachment && (
        <div className="flex items-center gap-2 border-t border-slate-100 bg-slate-50/70 px-5 py-3 text-[11px] text-slate-500">
          {busy ? <Loader2 size={12} className="animate-spin text-brand-600" /> : <Bot size={12} className="text-brand-600" />}
          {activeRequest
            ? "This step is completed in the open browser or with the action card above."
            : "The agent will ask here when it needs a decision or document."}
        </div>
      )}
    </section>
  );
}

function Message({ message }) {
  const { role, content, safe_metadata: meta, created_at: createdAt } = message;

  if (role === "system") {
    return (
      <div className="flex items-center gap-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
        <span className="h-px flex-1 bg-slate-200" />{content}<span className="h-px flex-1 bg-slate-200" />
      </div>
    );
  }

  const isUser = role === "user";
  const redacted = Boolean(meta?.secret_redacted);
  const screenshot = meta?.screenshot_data_uri;

  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : ""}`}>
      <div className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-lg ${
        isUser ? "bg-slate-200 text-slate-600" : "bg-brand-gradient text-white"
      }`}>
        {isUser ? <UserIcon size={13} /> : <Bot size={13} />}
      </div>
      <div className={`max-w-[85%] ${isUser ? "text-right" : ""}`}>
        <div className={`inline-block whitespace-pre-wrap rounded-xl px-3.5 py-2.5 text-left text-sm leading-relaxed ${
          redacted
            ? "bg-amber-50 text-amber-900 ring-1 ring-amber-200"
            : isUser
              ? "bg-slate-900 text-white"
              : "border border-brand-100 bg-brand-50/70 text-slate-800"
        }`}>
          {redacted && <ShieldAlert size={12} className="mr-1 inline-block align-[-2px]" />}
          {content}
        </div>
        {screenshot && (
          <img
            src={screenshot}
            alt="What Autogram saw in the browser when it needed help"
            className="mt-1.5 max-w-[320px] rounded-xl border border-slate-200 shadow-sm"
          />
        )}
        {createdAt && (
          <p className="mt-1 text-[10px] text-slate-400">
            {new Date(createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </p>
        )}
      </div>
    </div>
  );
}
