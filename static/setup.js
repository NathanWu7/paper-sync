/* PaperSync 设置向导前端 */

const $ = (sel) => document.querySelector(sel);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return r.json();
};

function showError(msg) {
  const el = $("#setup-error");
  el.textContent = msg;
  el.hidden = false;
}

async function loadDefaults() {
  const d = await api("/api/setup/defaults");
  const wrap = $("#providers");
  wrap.innerHTML = "";
  for (const [name, p] of Object.entries(d.providers)) {
    const card = document.createElement("div");
    card.className = "prov-card";
    const active = name === d.active ? "checked" : "";
    card.innerHTML = `
      <label class="prov-head">
        <input type="radio" name="active_provider" value="${name}" ${active}>
        <span class="prov-name">${name}</span>
      </label>
      <label class="prov-field">Base URL
        <input type="text" data-prov="${name}" data-k="base_url" value="${p.base_url || ""}">
      </label>
      <label class="prov-field">Model
        <input type="text" data-prov="${name}" data-k="model" value="${p.model || ""}">
      </label>
      <label class="prov-field">API Key
        <input type="password" data-prov="${name}" data-k="api_key" value="${p.api_key || ""}" placeholder="sk-…">
      </label>`;
    wrap.appendChild(card);
  }
}

async function loadStatus() {
  try {
    const st = await api("/api/status");
    if (st.ready) {
      $("#ready-repo").textContent = st.repo || "";
      $("#ready-banner").hidden = false;
    }
  } catch (e) { /* 未配置时正常走表单 */ }
}

function collectForm() {
  const providers = {};
  for (const input of document.querySelectorAll("#providers input[data-prov]")) {
    providers[input.dataset.prov] = providers[input.dataset.prov] || {};
    providers[input.dataset.prov][input.dataset.k] = input.value.trim();
  }
  return {
    repo_url: $("#repo-url").value.trim(),
    repo_path: $("#repo-path").value.trim(),
    github_token: $("#github-token").value.trim(),
    active_provider: document.querySelector('input[name="active_provider"]:checked')?.value || "kimi",
    providers,
  };
}

$("#setup-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#setup-submit");
  const body = collectForm();
  if (!body.repo_url && !body.repo_path) {
    showError("请填写 GitHub 链接或本地路径（二选一）");
    return;
  }
  if (body.repo_url && !/^(https:\/\/|git@)/.test(body.repo_url)) {
    showError("GitHub 链接需要以 https:// 或 git@ 开头");
    return;
  }
  btn.disabled = true;
  btn.textContent = "克隆并初始化中…";
  showError("");
  try {
    const d = await api("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (d.ok) location.replace("/");
  } catch (err) {
    showError(err.message);
    btn.disabled = false;
    btn.textContent = "开始校稿";
  }
});

(async function init() {
  await loadStatus();
  await loadDefaults();
})();
