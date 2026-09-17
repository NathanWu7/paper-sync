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
    wrap.appendChild(buildCard(name, p, false, name === d.active));
  }
}

function buildCard(name, p, custom, active) {
  const card = document.createElement("div");
  card.className = "prov-card" + (custom ? " prov-custom" : "");
  const head = custom
    ? `<label class="prov-head">
         <input type="radio" name="active_provider" value="${name}" ${active ? "checked" : ""}>
         <span class="prov-name">名称</span>
         <input type="text" class="prov-name-input" data-prov="${name}" data-k="__name" value="${name}" placeholder="例如 my-model">
         <button type="button" class="prov-remove" title="删除该模型">×</button>
       </label>`
    : `<label class="prov-head">
         <input type="radio" name="active_provider" value="${name}" ${active ? "checked" : ""}>
         <span class="prov-name">${name}</span>
       </label>`;
  card.innerHTML = head + `
      <label class="prov-field">Base URL
        <input type="text" data-prov="${name}" data-k="base_url" value="${p.base_url || ""}" placeholder="https://…/v1">
      </label>
      <label class="prov-field">Model
        <input type="text" data-prov="${name}" data-k="model" value="${p.model || ""}" placeholder="model 名称">
      </label>
      <label class="prov-field">API Key
        <input type="password" data-prov="${name}" data-k="api_key" value="${p.api_key || ""}" placeholder="sk-…">
      </label>`;
  if (custom) {
    const radio = card.querySelector('input[name="active_provider"]');
    const nameInput = card.querySelector(".prov-name-input");
    nameInput.addEventListener("input", () => {
      radio.value = nameInput.value.trim();
      nameInput.dataset.prov = nameInput.value.trim();
      for (const inp of card.querySelectorAll("input[data-k]")) inp.dataset.prov = nameInput.value.trim();
    });
    card.querySelector(".prov-remove").onclick = () => card.remove();
  }
  return card;
}

function addProvider() {
  const name = "custom-" + (document.querySelectorAll(".prov-custom").length + 1);
  $("#providers").appendChild(buildCard(name, {}, true, false));
}

$("#add-provider").onclick = addProvider;

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
  for (const card of document.querySelectorAll("#providers .prov-card")) {
    const nameInput = card.querySelector('input[data-k="__name"]');
    const name = nameInput
      ? nameInput.value.trim()
      : card.querySelector('input[name="active_provider"]').value;
    if (!name || providers[name]) continue;  // 空名/重名跳过
    providers[name] = {};
    for (const input of card.querySelectorAll("input[data-k]")) {
      if (input.dataset.k === "__name") continue;
      providers[name][input.dataset.k] = input.value.trim();
    }
  }
  return {
    repo_url: $("#repo-url").value.trim(),
    repo_path: $("#repo-path").value.trim(),
    github_token: $("#github-token").value.trim(),
    active_provider: document.querySelector('input[name="active_provider"]:checked')?.value || "deepseek",
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
  if (body.providers[body.active_provider] && !body.providers[body.active_provider].api_key) {
    showError(`所选模型「${body.active_provider}」的 API Key 为空，请填写`);
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
