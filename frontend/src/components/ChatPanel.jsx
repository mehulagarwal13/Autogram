import { useCallback, useEffect, useRef, useState } from "react";
import { Bot, Loader2, MessageSquare, Send, ShieldAlert, User as UserIcon, Wifi, WifiOff } from "lucide-react";
import { api, openChatStream } from "../api";

/**
 * The conversation for ONE automation attempt.
 *
 * Data flow, and why it is shaped this way:
 *
 *   transcript (HTTP)  = the authority. Fetched on mount and after every event.
 *   event stream (WS)  = an accelerator. Each event only means "something
 *                        changed, go look" — never carries the transcript.
 *
 * Keeping the socket purely as a trigger is what makes a dropped event, a
 * closed laptop lid, or a proxy timeout harmless: the next refetch shows the
 * truth. If messages were appended straight from event payloads instead, any
 * missed frame would leave the panel permanently and invisibly wrong.
 *
 * Answering a human-in-the-loop prompt deliberately goes through the existing
 * `/human-requests/{id}/respond` route, not a chat-specific one — that route
 * owns the atomic claim that stops two concurrent answers from resuming one
 * task twice.
 */
export default function ChatPanel({ scope, resourceId, activeRequest, onRespond, busy }) {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(true);
  const [live, setLive] = useState(false);
  const [draft, setDraft] = useState("");
  const bottomRef = useRef(null);
  const pollRef = useRef(null);

  const refresh = useCallback(async () => {
    try {
      setMessages(await api.getChatTranscript(scope, resourceId));
    } catch {
      /* transient — the next event or poll tick retries */
    } finally {
      setLoading(false);
    }
  }, [scope, resourceId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    const socket = openChatStream(scope, resourceId, () => refresh(), {
      onError: () => setLive(false),
    });
    if (socket) {
      socket.onopen = () => setLive(true);
      socket.onclose = () => setLive(false);
    }

    // Polling fallback, deliberately kept even when the socket is up but at a
    // slow cadence: a WebSocket can be blocked by a corporate proxy, and an
    // application that silently stops updating is worse than one that updates
    // late. The socket makes this fast; it is not what makes it correct.
    pollRef.current = setInterval(refresh, 15000);
    return () => {
      clearInterval(pollRef.current);
      socket?.close();
    };
  }, [scope, resourceId, refresh]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  function submitDraft(e) {
    e?.preventDefault();
    const text = draft.trim();
    if (!text || busy) return;
    onRespond?.(text);
    setDraft("");
  }

  // Free-text is only offered when the workflow is actually waiting on a
  // free-text answer. A chat box that accepts input the backend will discard
  // teaches users their messages do something when they do not.
  const acceptsFreeText = activeRequest?.request_type === "ANSWER_REQUIRED";

  return (
    <div className="card flex h-full flex-col overflow-hidden">
      <div className="card-header flex items-center justify-between">
        <h2 className="flex items-center gap-2 font-semibold text-slate-900">
          <MessageSquare size={16} /> Conversation
        </h2>
        <span
          className="flex items-center gap-1 text-[11px] text-slate-500"
          title={live ? "Live updates connected" : "Live updates unavailable — refreshing periodically instead"}
        >
          {live ? <Wifi size={12} className="text-emerald-600" /> : <WifiOff size={12} />}
          {live ? "Live" : "Polling"}
        </span>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {loading && (
          <p className="flex items-center gap-2 text-xs text-slate-500">
            <Loader2 size={13} className="animate-spin" /> Loading conversation...
          </p>
        )}
        {!loading && messages.length === 0 && (
          <p className="text-xs text-slate-500">
            Nothing yet — messages appear here as the automation runs and whenever it needs you.
          </p>
        )}
        {messages.map((m) => (
          <Message key={m.message_id} message={m} />
        ))}
        <div ref={bottomRef} />
      </div>

      {acceptsFreeText && (
        <form onSubmit={submitDraft} className="border-t border-slate-200 p-3">
          <p className="mb-2 text-xs font-medium text-slate-700">{activeRequest.message}</p>
          <div className="flex gap-2">
            <input
              className="input flex-1 !py-2 text-sm"
              placeholder="Type your answer..."
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={busy}
              autoFocus
            />
            <button type="submit" className="btn-primary !py-2" disabled={busy || !draft.trim()}>
              {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}

function Message({ message }) {
  const { role, content, safe_metadata: meta, created_at: createdAt } = message;

  if (role === "system") {
    return (
      <p className="text-center text-[11px] uppercase tracking-wide text-slate-400">{content}</p>
    );
  }

  const isUser = role === "user";
  // A redacted verification code is styled distinctly so the transcript makes
  // it obvious that Autogram stored the FACT of a code, never the code itself.
  const redacted = Boolean(meta?.secret_redacted);
  // Attached only when the agent fell back to a vision-assisted screenshot
  // before pausing (spec §19/§21/§24) — a small inline data URI, no separate
  // fetch or storage endpoint needed.
  const screenshot = meta?.screenshot_data_uri;

  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : ""}`}>
      <div
        className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${
          isUser ? "bg-slate-200 text-slate-600" : "bg-brand-600 text-white"
        }`}
      >
        {isUser ? <UserIcon size={13} /> : <Bot size={13} />}
      </div>
      <div className={`max-w-[80%] ${isUser ? "text-right" : ""}`}>
        <div
          className={`inline-block rounded-lg px-3 py-2 text-sm ${
            redacted
              ? "bg-amber-50 text-amber-900 ring-1 ring-amber-200"
              : isUser
                ? "bg-slate-100 text-slate-800"
                : "bg-brand-50 text-slate-800"
          }`}
        >
          {redacted && <ShieldAlert size={12} className="mr-1 inline-block align-[-2px]" />}
          {content}
        </div>
        {screenshot && (
          <img
            src={screenshot}
            alt="What Autogram saw in the browser when it needed help"
            className="mt-1.5 max-w-[320px] rounded-lg border border-slate-200 shadow-sm"
          />
        )}
        {createdAt && (
          <p className="mt-0.5 text-[10px] text-slate-400">
            {new Date(createdAt).toLocaleTimeString()}
          </p>
        )}
      </div>
    </div>
  );
}
