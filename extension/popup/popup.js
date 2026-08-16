// Popup UI logic. Talks only to background.js via chrome.runtime.sendMessage
// — never calls the backend directly, never touches the page's DOM itself.

function send(message) {
  return chrome.runtime.sendMessage(message);
}

const loginView = document.getElementById("login-view");
const mainView = document.getElementById("main-view");
const loginError = document.getElementById("login-error");
const mainError = document.getElementById("main-error");
const progressEl = document.getElementById("progress");
const resultEl = document.getElementById("result");

function showError(el, message) {
  el.textContent = message;
  el.classList.remove("hidden");
}

function hideError(el) {
  el.classList.add("hidden");
}

async function refreshView() {
  const { config } = await send({ type: "GET_CONFIG" });
  if (config.token) {
    loginView.classList.add("hidden");
    mainView.classList.remove("hidden");
    document.getElementById("account-email").textContent = config.email || "";
    document.getElementById("autopilot-toggle").checked = !!config.autopilotEnabled;
  } else {
    mainView.classList.add("hidden");
    loginView.classList.remove("hidden");
  }
}

document.getElementById("login-btn").addEventListener("click", async () => {
  hideError(loginError);
  const email = document.getElementById("login-email").value.trim();
  const password = document.getElementById("login-password").value;
  if (!email || !password) return showError(loginError, "Enter your email and password.");

  const btn = document.getElementById("login-btn");
  btn.disabled = true;
  btn.textContent = "Signing in...";
  try {
    const res = await send({ type: "LOGIN", email, password });
    if (!res.ok) throw new Error(res.error);
    await refreshView();
  } catch (e) {
    showError(loginError, e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Sign In";
  }
});

document.getElementById("logout-link").addEventListener("click", async (e) => {
  e.preventDefault();
  await send({ type: "LOGOUT" });
  await refreshView();
});

document.getElementById("settings-btn").addEventListener("click", () => {
  chrome.runtime.openOptionsPage();
});

document.getElementById("fill-btn").addEventListener("click", async () => {
  hideError(mainError);
  resultEl.classList.add("hidden");
  progressEl.classList.remove("hidden");
  document.getElementById("progress-text").textContent = "Working — this can take a few seconds...";
  const fillBtn = document.getElementById("fill-btn");
  fillBtn.disabled = true;

  try {
    const res = await send({ type: "FILL_THIS_APPLICATION" });
    if (!res.ok) throw new Error(res.error);
    const { result } = res;

    const STATUS_TEXT = {
      copilot_review: "Filled and ready for your review — click Submit on the real page once you've checked it.",
      needs_review: "Some answers need your review before this is ready to submit.",
      manual_required: "Waiting on you to complete a verification challenge on the page.",
    };
    document.getElementById("result-text").textContent =
      STATUS_TEXT[result.status] || `Status: ${result.status}`;
    if (result.resumeFieldFound) {
      document.getElementById("result-text").textContent +=
        " Don't forget to attach your résumé — browsers don't allow extensions to do that step automatically.";
    }
    document.getElementById("result-link").href = `${result.frontendUrl}/applications/${result.applicationId}`;
    resultEl.classList.remove("hidden");
  } catch (e) {
    showError(mainError, e.message);
  } finally {
    progressEl.classList.add("hidden");
    fillBtn.disabled = false;
  }
});

refreshView();
