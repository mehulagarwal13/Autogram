/**
 * API client contract.
 *
 * The properties tested here are the ones whose failure is SILENT: a structured
 * 409 that loses its machine-readable reason renders as raw JSON in a toast; a
 * live stream that drops KEEPALIVE frames upward makes every consumer
 * re-implement the same filter; a token that never reaches the socket URL means
 * the panel falls back to polling forever with no visible error.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { api, auth, openChatStream } from "../api";

function mockFetch(status, body) {
  globalThis.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  });
}

describe("error handling", () => {
  beforeEach(() => auth.setToken("test-token"));

  it("keeps a structured 409 detail available to the caller", async () => {
    // The duplicate/re-application flows branch on `reason`. If the client
    // flattened this to a string, the UI could only show prose and would have
    // to parse it back out.
    mockFetch(409, {
      detail: {
        reason: "application_already_submitted",
        message: "You have already submitted an application for this job.",
        application_id: "app_1",
      },
    });

    await expect(api.startApplication({ job_url: "https://x.test/j" })).rejects.toMatchObject({
      message: "You have already submitted an application for this job.",
      detail: { reason: "application_already_submitted", application_id: "app_1" },
    });
  });

  it("surfaces a plain string detail as the message", async () => {
    mockFetch(409, { detail: "Application is already in progress." });
    await expect(api.startApplication({ job_url: "https://x.test/j" })).rejects.toThrow(
      "Application is already in progress.",
    );
  });

  it("never surfaces a raw backend traceback to the user", async () => {
    // A 500 body can contain anything. The user must get a generic message.
    mockFetch(500, { detail: 'Traceback (most recent call last):\n  File "app.py"' });
    try {
      await api.startApplication({ job_url: "https://x.test/j" });
      throw new Error("should have rejected");
    } catch (e) {
      expect(e.message).not.toContain("Traceback");
    }
  });

  it("clears the stored token on 401 so a dead session cannot linger", async () => {
    mockFetch(401, { detail: "Invalid or expired token." });
    await expect(api.listApplications()).rejects.toThrow(/Session expired/);
    expect(auth.getToken()).toBeNull();
  });
});

describe("chat stream", () => {
  beforeEach(() => auth.setToken("tok-123"));

  it("does not open a socket when there is no token", () => {
    auth.clear();
    expect(openChatStream("tasks", "t1", () => {})).toBeNull();
  });

  it("authenticates via the query string, because browsers cannot set a WS header", () => {
    openChatStream("tasks", "t1", () => {});
    const socket = globalThis.MockWebSocket.instances.at(-1);
    expect(socket.url).toContain("/api/chat/tasks/t1/stream");
    expect(socket.url).toContain("token=tok-123");
  });

  it("swallows KEEPALIVE frames instead of handing them to consumers", () => {
    // These exist only to stop proxies dropping an idle connection. Passing
    // them upward would make every caller re-implement the same filter, and one
    // that forgot would refetch every 25 seconds forever.
    const onEvent = vi.fn();
    openChatStream("tasks", "t1", onEvent);
    const socket = globalThis.MockWebSocket.instances.at(-1);

    socket.emit({ event: "KEEPALIVE" });
    expect(onEvent).not.toHaveBeenCalled();

    socket.emit({ event: "FIELD_FILLED", payload: { field: "email" } });
    expect(onEvent).toHaveBeenCalledWith(
      expect.objectContaining({ event: "FIELD_FILLED" }),
    );
  });

  it("survives an unparseable frame without tearing down the stream", () => {
    const onEvent = vi.fn();
    openChatStream("tasks", "t1", onEvent);
    const socket = globalThis.MockWebSocket.instances.at(-1);

    socket.onmessage({ data: "<!doctype html>not json" });
    expect(onEvent).not.toHaveBeenCalled();

    socket.emit({ event: "APPLICATION_SUBMITTED" });
    expect(onEvent).toHaveBeenCalledTimes(1);
  });
});

describe("secret hygiene", () => {
  it("persists only the auth token — never a code, password, or cookie", () => {
    auth.setToken("tok-abc");
    const keys = Object.keys(localStorage);
    expect(keys).toHaveLength(1);
    // Whatever the key is called, its value must be the token and nothing else.
    expect(localStorage.getItem(keys[0])).toBe("tok-abc");
  });
});
