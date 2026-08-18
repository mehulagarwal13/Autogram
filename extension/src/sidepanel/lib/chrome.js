// Thin wrapper around chrome.runtime.sendMessage — every call to
// background.js goes through here. The side panel never calls the backend
// or touches a tab directly; background.js owns all of that (see its own
// docstring for why).

export function sendMessage(type, payload = {}) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ type, ...payload }, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!response?.ok) {
        reject(new Error(response?.error || "Unknown error"));
        return;
      }
      resolve(response);
    });
  });
}

export function onProgress(callback) {
  const listener = (message) => {
    if (message.type === "FILL_PROGRESS") callback(message.status);
  };
  chrome.runtime.onMessage.addListener(listener);
  return () => chrome.runtime.onMessage.removeListener(listener);
}
