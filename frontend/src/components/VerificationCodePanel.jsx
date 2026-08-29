import { useRef, useState } from "react";
import { KeyRound, Loader2, ShieldCheck } from "lucide-react";
import { api } from "../api";

/**
 * Where the user types a one-time passcode for a DETERMINISTIC application run.
 *
 * Shown only when the backend says this application is genuinely paused on a
 * verification gate. Autogram never asks for a code speculatively, and never
 * asks for a password — an application form that wants one is an account wall,
 * which the automation refuses to transact with at all.
 *
 * SECRET HANDLING — the code:
 *   - lives in component state only while being typed,
 *   - is cleared the instant it is handed to the API (also blocking a
 *     double-submit, since the field is empty again),
 *   - is never written to localStorage, never put in a URL, never logged,
 *     and never echoed back by the response,
 *   - is not added to the chat transcript: the backend records only the FACT
 *     that a code was supplied.
 *
 * The one thing this component must never imply is that Autogram can obtain the
 * code itself. It cannot, and must not: the code goes to the user's own phone or
 * inbox, and reading it from there is exactly the boundary this product does not
 * cross.
 */
export default function VerificationCodePanel({ applicationId, onSubmitted, rejected }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [sent, setSent] = useState(false);
  const inputRef = useRef(null);

  async function submit(e) {
    e?.preventDefault();
    const value = code.trim();
    if (!value || busy) return;

    setBusy(true);
    setError(null);
    try {
      await api.submitVerificationCode(applicationId, value);
      // Clear BEFORE anything else can re-render with it still in state.
      setCode("");
      setSent(true);
      onSubmitted?.();
    } catch (err) {
      setError(err.message);
      // Deliberately keep the typed value on failure so a transient network
      // error does not force the user to re-read a code from their phone —
      // but select it, so a retype replaces rather than appends.
      inputRef.current?.select();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card border-amber-200 bg-amber-50/60 p-5">
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-amber-100">
          <KeyRound size={17} className="text-amber-700" />
        </div>
        <div className="flex-1">
          <h3 className="font-semibold text-slate-900">Enter your verification code</h3>
          <p className="mt-1 text-sm text-slate-600">
            The application is waiting on a one-time passcode. Check the email or phone the employer
            sent it to, then enter it below — the automation will type it into the form and carry on.
          </p>

          <form onSubmit={submit} className="mt-4 flex flex-wrap items-center gap-2">
            <input
              ref={inputRef}
              className="input !w-44 !py-2 text-center font-mono text-lg tracking-[0.3em]"
              // `one-time-code` lets iOS/Android offer the code from the SMS
              // itself, which is the one autofill that genuinely helps here.
              autoComplete="one-time-code"
              inputMode="numeric"
              placeholder="000000"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\s/g, ""))}
              disabled={busy}
              maxLength={12}
              autoFocus
            />
            <button type="submit" className="btn-primary !py-2" disabled={busy || !code.trim()}>
              {busy ? <Loader2 size={15} className="animate-spin" /> : <ShieldCheck size={15} />}
              {busy ? "Sending..." : "Submit code"}
            </button>
          </form>

          {error && <p className="mt-2 text-xs text-red-600">{error}</p>}

          {/* The backend sets `verification_rejected` on LIVE_RUN_STATE after
              observing that the gate is STILL there post-submit. Showing it
              here is what stops a mistyped code from looking identical to
              "we're still waiting for you to type one" — otherwise the user
              waits for automation that is waiting for them. */}
          {rejected && !busy && (
            <p className="mt-2 text-xs font-medium text-red-600">
              That code wasn't accepted. Check for a newer one and enter it above.
            </p>
          )}

          {sent && !error && !rejected && (
            <p className="mt-2 flex items-center gap-1.5 text-xs text-emerald-700">
              <ShieldCheck size={13} />
              Code sent. If it was wrong, the form will ask again — enter the new one here.
            </p>
          )}

          <p className="mt-3 border-t border-amber-200 pt-3 text-[11px] leading-relaxed text-slate-500">
            Autogram never stores this code, never logs it, and never sends it to an AI model. It is
            held in memory only long enough to type into the form, then discarded. You can also enter
            it directly in the automation's browser window instead — either works.
          </p>
        </div>
      </div>
    </div>
  );
}
