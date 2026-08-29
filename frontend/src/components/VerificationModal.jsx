import { useEffect, useMemo, useRef, useState } from "react";
import { KeyRound, Loader2, ShieldAlert, Ban, ArrowRight } from "lucide-react";
import Modal from "./Modal";

const CODE_LENGTH = 6;

function useCountdown(expiresAt) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!expiresAt) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [expiresAt]);
  if (!expiresAt) return null;
  const remainingMs = new Date(expiresAt).getTime() - now;
  return Math.max(0, Math.floor(remainingMs / 1000));
}

function formatCountdown(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * OTP / MFA verification modal. `request` is a `HumanInteractionRequest`
 * (see `app/api/human_interaction.py`): { request_id, request_type, message,
 * safe_metadata: { masked_destination }, expires_at }. Never handles a
 * request whose type isn't OTP_REQUIRED/MFA_REQUIRED — the caller decides
 * which UI to show based on `request_type`.
 */
export default function VerificationModal({ request, onSubmit, onCancel, submitting, error }) {
  const [digits, setDigits] = useState(Array(CODE_LENGTH).fill(""));
  const inputRefs = useRef([]);
  const secondsLeft = useCountdown(request?.expires_at);
  const expired = secondsLeft !== null && secondsLeft <= 0;

  useEffect(() => {
    inputRefs.current[0]?.focus();
  }, []);

  const code = useMemo(() => digits.join(""), [digits]);
  const isComplete = code.length === CODE_LENGTH && /^\d+$/.test(code);

  function setDigitAt(index, value) {
    setDigits((prev) => {
      const next = [...prev];
      next[index] = value;
      return next;
    });
  }

  function handleChange(index, raw) {
    const value = raw.replace(/\D/g, "");
    if (!value) {
      setDigitAt(index, "");
      return;
    }
    // Typing (or pasting into one box) more than one digit — spread across
    // this box and the following ones, same behavior as native OTP inputs.
    const chars = value.split("");
    setDigits((prev) => {
      const next = [...prev];
      chars.forEach((ch, i) => {
        if (index + i < CODE_LENGTH) next[index + i] = ch;
      });
      return next;
    });
    const lastFilled = Math.min(index + chars.length, CODE_LENGTH - 1);
    inputRefs.current[lastFilled]?.focus();
  }

  function handleKeyDown(index, e) {
    if (e.key === "Backspace" && !digits[index] && index > 0) {
      inputRefs.current[index - 1]?.focus();
    }
    if (e.key === "ArrowLeft" && index > 0) inputRefs.current[index - 1]?.focus();
    if (e.key === "ArrowRight" && index < CODE_LENGTH - 1) inputRefs.current[index + 1]?.focus();
  }

  function handlePaste(e) {
    e.preventDefault();
    const pasted = (e.clipboardData.getData("text") || "").replace(/\D/g, "").slice(0, CODE_LENGTH);
    if (!pasted) return;
    setDigits(Array.from({ length: CODE_LENGTH }, (_, i) => pasted[i] || ""));
    inputRefs.current[Math.min(pasted.length, CODE_LENGTH - 1)]?.focus();
  }

  function clearCode() {
    setDigits(Array(CODE_LENGTH).fill(""));
  }

  function handleSubmit() {
    if (!isComplete || submitting || expired) return;
    onSubmit(code);
    // Drop the code from component state the instant it's handed to the API.
    // Also makes `isComplete` false again, which is what blocks a
    // double-click from submitting the same code twice.
    clearCode();
  }

  function handleCancel() {
    // Clear before cancelling: if the cancel request itself fails, the modal
    // stays mounted, and the typed code must not still be sitting in state.
    clearCode();
    onCancel();
  }

  // Dismissing the modal (backdrop click / X) is deliberately NON-destructive
  // and separate from "Cancel application". Wiring the backdrop to onCancel
  // meant one stray click outside the dialog silently cancelled the user's
  // whole job application — an irreversible action from an accidental gesture.
  // The task is genuinely blocked while this request is pending, so dismissing
  // just discards whatever was typed; cancelling requires the explicit button.
  function handleDismiss() {
    clearCode();
  }

  const isMfa = request?.request_type === "MFA_REQUIRED";
  const maskedDestination = request?.safe_metadata?.masked_destination;

  return (
    <Modal title="Verification Required" onClose={handleDismiss}>
      <div className="flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-amber-50">
          <KeyRound size={17} className="text-amber-600" />
        </div>
        <div className="flex-1">
          <p className="text-sm text-slate-700">
            {request?.message || (isMfa
              ? "This application requires a two-factor authentication code to continue."
              : "This application requires a one-time verification code to continue.")}
          </p>
          {maskedDestination && (
            <p className="mt-1.5 text-xs text-slate-500">A code may have been sent to {maskedDestination}.</p>
          )}
        </div>
      </div>

      <div className="mt-5">
        <label className="mb-2 block text-xs font-medium text-slate-600">Verification code</label>
        <div className="flex gap-2" onPaste={handlePaste}>
          {digits.map((d, i) => (
            <input
              key={i}
              ref={(el) => (inputRefs.current[i] = el)}
              inputMode="numeric"
              autoComplete={i === 0 ? "one-time-code" : "off"}
              maxLength={CODE_LENGTH}
              value={d}
              disabled={submitting || expired}
              onChange={(e) => handleChange(i, e.target.value)}
              onKeyDown={(e) => handleKeyDown(i, e)}
              className="input h-12 w-11 !p-0 text-center text-lg font-semibold tracking-widest disabled:opacity-60"
            />
          ))}
        </div>

        <div className="mt-2.5 flex items-center justify-between text-xs">
          {secondsLeft !== null ? (
            <span className={expired ? "font-medium text-red-600" : "text-slate-500"}>
              {expired ? "This code has expired." : `Expires in ${formatCountdown(secondsLeft)}`}
            </span>
          ) : (
            <span />
          )}
        </div>
      </div>

      {error && (
        <div className="mt-3 flex items-start gap-2 rounded-lg bg-red-50 p-3 text-xs text-red-700">
          <ShieldAlert size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <div className="mt-5 flex justify-end gap-2">
        <button onClick={handleCancel} disabled={submitting} className="btn-secondary flex items-center gap-2">
          <Ban size={14} /> Cancel application
        </button>
        <button onClick={handleSubmit} disabled={!isComplete || submitting || expired} className="btn-primary flex items-center gap-2">
          {submitting ? <Loader2 size={14} className="animate-spin" /> : <ArrowRight size={14} />}
          Continue
        </button>
      </div>
    </Modal>
  );
}
