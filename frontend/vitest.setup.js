// Test environment setup for the frontend suite.
//
// `jest-dom` adds the DOM-aware matchers (toBeInTheDocument, toBeDisabled)
// these tests read much better with.
import "@testing-library/jest-dom/vitest";

// jsdom implements no WebSocket at all. Every component that opens the live
// chat stream would otherwise throw on mount, so the suite would be testing
// error paths rather than behaviour. This stub records what was opened so a
// test can assert on it, and stays inert unless a test drives it.
class MockWebSocket {
  static instances = [];
  static OPEN = 1;

  constructor(url) {
    this.url = url;
    this.readyState = MockWebSocket.OPEN;
    this.onmessage = null;
    this.onerror = null;
    this.onopen = null;
    this.onclose = null;
    this.closed = false;
    MockWebSocket.instances.push(this);
  }

  // Let a test deliver a frame exactly as the server would.
  emit(payload) {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }

  close() {
    this.closed = true;
    this.onclose?.();
  }
}

// jsdom implements no layout, so `scrollIntoView` does not exist on Element.
// Any component that keeps a message list pinned to the bottom would throw on
// every render. Stubbed rather than guarded in the component: scrolling to the
// newest message is correct product behaviour, and adding a defensive
// `typeof === "function"` check there would be test-shaped code in production.
Element.prototype.scrollIntoView = () => {};

globalThis.WebSocket = MockWebSocket;
globalThis.MockWebSocket = MockWebSocket;

beforeEach(() => {
  MockWebSocket.instances.length = 0;
  localStorage.clear();
});
