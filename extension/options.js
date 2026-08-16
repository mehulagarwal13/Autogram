async function load() {
  const { config } = await chrome.runtime.sendMessage({ type: "GET_CONFIG" });
  document.getElementById("backend-url").value = config.backendUrl;
  document.getElementById("frontend-url").value = config.frontendUrl;
}

document.getElementById("save-btn").addEventListener("click", async () => {
  const backendUrl = document.getElementById("backend-url").value.trim().replace(/\/$/, "");
  const frontendUrl = document.getElementById("frontend-url").value.trim().replace(/\/$/, "");
  await chrome.runtime.sendMessage({ type: "SET_CONFIG", config: { backendUrl, frontendUrl } });
  const msg = document.getElementById("saved-msg");
  msg.style.display = "block";
  setTimeout(() => (msg.style.display = "none"), 2000);
});

load();
