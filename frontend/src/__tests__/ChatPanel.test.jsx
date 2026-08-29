/**
 * ChatPanel — the workflow-aware conversation surface.
 *
 * The behaviours worth pinning are the ones that would be wrong in a way nobody
 * notices:
 *
 *  - a submitted verification code must render as a REDACTION, never as the
 *    code, because this transcript is refetched on every page load;
 *  - the free-text box must appear only when the workflow is actually waiting
 *    on free text, or the UI teaches users that typing does something when the
 *    backend will discard it;
 *  - a live event must trigger a REFETCH rather than being appended, so a
 *    dropped frame leaves the panel stale rather than permanently wrong.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ChatPanel from "../components/ChatPanel";
import { api, auth } from "../api";

function message(overrides = {}) {
  return {
    message_id: `m_${Math.random()}`,
    role: "agent",
    content: "Hello",
    human_request_id: null,
    safe_metadata: null,
    created_at: "2026-08-29T10:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  auth.setToken("tok");
  vi.restoreAllMocks();
});

describe("transcript rendering", () => {
  it("shows the agent's question and the user's reply", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([
      message({ role: "agent", content: "What is your notice period?" }),
      message({ role: "user", content: "30 days" }),
    ]);

    render(<ChatPanel scope="tasks" resourceId="t1" />);

    expect(await screen.findByText("What is your notice period?")).toBeInTheDocument();
    expect(screen.getByText("30 days")).toBeInTheDocument();
  });

  it("renders a submitted code as a redaction, never as the code", async () => {
    // The backend stores a fixed placeholder, never the digits. This asserts
    // the UI does not somehow reconstruct or reveal one.
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([
      message({
        role: "user",
        content: "Verification code submitted.",
        safe_metadata: { request_type: "OTP_REQUIRED", secret_redacted: true },
      }),
    ]);

    const { container } = render(<ChatPanel scope="tasks" resourceId="t1" />);

    expect(await screen.findByText("Verification code submitted.")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\d{4,}/);
  });

  it("explains an empty transcript instead of showing a blank panel", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    render(<ChatPanel scope="tasks" resourceId="t1" />);
    expect(await screen.findByText(/Nothing yet/i)).toBeInTheDocument();
  });

  it("stays usable when the transcript request fails", async () => {
    // A transient API failure must not blank the panel or throw — the poll
    // and the next event both retry.
    vi.spyOn(api, "getChatTranscript").mockRejectedValue(new Error("network"));
    render(<ChatPanel scope="tasks" resourceId="t1" />);
    await waitFor(() => expect(screen.getByText(/Nothing yet/i)).toBeInTheDocument());
  });
});

describe("workflow-aware input", () => {
  it("offers free text only when the workflow is waiting for an answer", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);

    const { rerender } = render(<ChatPanel scope="tasks" resourceId="t1" activeRequest={null} />);
    await screen.findByText(/Nothing yet/i);
    expect(screen.queryByPlaceholderText(/Type your answer/i)).toBeNull();

    rerender(
      <ChatPanel
        scope="tasks"
        resourceId="t1"
        activeRequest={{ request_type: "ANSWER_REQUIRED", message: "Expected salary?" }}
      />,
    );
    expect(await screen.findByPlaceholderText(/Type your answer/i)).toBeInTheDocument();
    expect(screen.getByText("Expected salary?")).toBeInTheDocument();
  });

  it("does NOT offer free text for a CAPTCHA pause", async () => {
    // A CAPTCHA is cleared in the browser, not by typing. A text box here would
    // invite an answer the backend has nowhere to put.
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    render(
      <ChatPanel
        scope="tasks"
        resourceId="t1"
        activeRequest={{ request_type: "CAPTCHA_REQUIRED", message: "Solve the CAPTCHA." }}
      />,
    );
    await screen.findByText(/Nothing yet/i);
    expect(screen.queryByPlaceholderText(/Type your answer/i)).toBeNull();
  });

  it("hands the typed answer to the caller and clears the box", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    const onRespond = vi.fn();
    render(
      <ChatPanel
        scope="tasks"
        resourceId="t1"
        activeRequest={{ request_type: "ANSWER_REQUIRED", message: "Expected salary?" }}
        onRespond={onRespond}
      />,
    );

    const box = await screen.findByPlaceholderText(/Type your answer/i);
    await userEvent.type(box, "  £70,000  ");
    await userEvent.keyboard("{Enter}");

    expect(onRespond).toHaveBeenCalledWith("£70,000"); // trimmed
    expect(box).toHaveValue("");
  });

  it("refuses to submit an empty or whitespace-only answer", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    const onRespond = vi.fn();
    render(
      <ChatPanel
        scope="tasks"
        resourceId="t1"
        activeRequest={{ request_type: "ANSWER_REQUIRED", message: "Expected salary?" }}
        onRespond={onRespond}
      />,
    );
    const box = await screen.findByPlaceholderText(/Type your answer/i);
    await userEvent.type(box, "   ");
    await userEvent.keyboard("{Enter}");
    expect(onRespond).not.toHaveBeenCalled();
  });

  it("does not submit while a previous response is still in flight", async () => {
    // Double-submitting an answer races the backend's atomic resume claim; the
    // loser gets a 409. Blocking it here keeps that out of the user's face.
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    const onRespond = vi.fn();
    render(
      <ChatPanel
        scope="tasks"
        resourceId="t1"
        activeRequest={{ request_type: "ANSWER_REQUIRED", message: "Expected salary?" }}
        onRespond={onRespond}
        busy
      />,
    );
    const box = await screen.findByPlaceholderText(/Type your answer/i);
    expect(box).toBeDisabled();
  });
});

describe("live updates", () => {
  it("refetches the transcript when an event arrives, rather than appending", async () => {
    // The socket is a trigger, not a data source. If events were appended
    // directly, one dropped frame would leave the panel permanently wrong
    // instead of merely a few seconds stale.
    const fetchTranscript = vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    render(<ChatPanel scope="tasks" resourceId="t1" />);
    await screen.findByText(/Nothing yet/i);

    const before = fetchTranscript.mock.calls.length;
    fetchTranscript.mockResolvedValue([message({ content: "Filling page 2 of 3" })]);
    globalThis.MockWebSocket.instances.at(-1).emit({ event: "FIELD_FILLED", payload: {} });

    await waitFor(() => expect(fetchTranscript.mock.calls.length).toBeGreaterThan(before));
    expect(await screen.findByText("Filling page 2 of 3")).toBeInTheDocument();
  });

  it("shows connection state so a dead stream is visible, not silent", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    render(<ChatPanel scope="tasks" resourceId="t1" />);
    await screen.findByText(/Nothing yet/i);

    const socket = globalThis.MockWebSocket.instances.at(-1);
    socket.onopen?.();
    expect(await screen.findByText("Live")).toBeInTheDocument();

    socket.close();
    expect(await screen.findByText("Polling")).toBeInTheDocument();
  });

  it("closes its socket on unmount so a long session does not leak subscribers", async () => {
    vi.spyOn(api, "getChatTranscript").mockResolvedValue([]);
    const { unmount } = render(<ChatPanel scope="tasks" resourceId="t1" />);
    await screen.findByText(/Nothing yet/i);

    const socket = globalThis.MockWebSocket.instances.at(-1);
    unmount();
    expect(socket.closed).toBe(true);
  });
});
