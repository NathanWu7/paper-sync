/* PaperSync 中英双语编辑器前端 */

let FILES = [];
let currentFile = null;
let provider = "kimi";

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

/* ---------- 提示 ---------- */
function toast(msg, ms = 3000) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), ms);
}
function showConsole(text) {
  const c = $("#console");
  c.hidden = false;
  c.textContent = text;
  c.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

/* ---------- KaTeX ---------- */
function renderMath(root) {
  root.querySelectorAll("span.math").forEach((el) => {
    if (el.dataset.rendered) return;
    const display = el.classList.contains("display");
    try {
      katex.render(el.textContent, el, { displayMode: display, throwOnError: false });
    } catch (e) { /* 保留原文 */ }
    el.dataset.rendered = "1";
  });
}

/* ---------- 文件页签 ---------- */
async function loadFiles() {
  const d = await api("/api/files");
  FILES = d.files;
  const tabs = $("#tabs");
  tabs.innerHTML = "";
  for (const f of FILES) {
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.path = f.path;
    const name = f.path.split("/").pop().replace(/\.tex$/, "") === "main"
      ? "main(摘要)" : f.path.split("/").pop().replace(/\.tex$/, "");
    b.innerHTML = name +
      (f.n_warn ? ` <span class="cnt warn">${f.n_warn}</span>` : "") +
      (f.n_pending ? ` <span class="cnt">${f.n_pending}</span>` : "");
    b.onclick = () => loadSegments(f.path);
    tabs.appendChild(b);
  }
  if (!currentFile && FILES.length) loadSegments(FILES[0].path);
}

/* ---------- 段落 ---------- */
const STATUS_ICON = {
  synced: ["✓", "已同步", "ok"],
  untranslated: ["●", "未翻译", "gray"],
  en_changed: ["⇅", "英文已更新，中文过时", "orange"],
  warn: ["⚠", "校验警告", "red"],
  translating: ["↻", "翻译中…", "spin"],
};

async function loadSegments(file) {
  currentFile = file;
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.path === file));
  const d = await api("/api/segments?file=" + encodeURIComponent(file));
  const wrap = $("#segments");
  wrap.innerHTML = "";
  for (const seg of d.segments) wrap.appendChild(buildRow(seg));
  renderMath(wrap);
  $("#current-file").textContent = file;
  wrap.classList.remove("reveal"); void wrap.offsetWidth; wrap.classList.add("reveal");
  if (window.loadChatHistory) loadChatHistory(file);
}

function buildRow(seg) {
  const row = document.createElement("div");
  row.className = "row kind-" + seg.kind;
  row.dataset.idx = seg.idx;

  const [icon, tip, cls] = STATUS_ICON[seg.status] || STATUS_ICON.untranslated;
  const zh = document.createElement("div");
  zh.className = "cell col-zh";
  const badge = `<span class="badge ${cls}" title="${tip}${seg.warnings.length ? "&#10;" + seg.warnings.join("&#10;") : ""}">${icon}</span>`;
  zh.innerHTML = badge + `<div class="body">${
    seg.zh ? seg.zh_html : '<span class="empty">（暂无中文，点击翻译或手动输入）</span>'
  }</div>`;
  zh.title = "点击编辑中文";
  zh.onclick = () => startEdit(row, seg);
  if (seg.status === "en_changed" || seg.status === "untranslated") {
    const btn = document.createElement("button");
    btn.className = "mini zh-auto";
    btn.textContent = "⇥ 英→中";
    btn.title = "自动翻译英文到中文";
    btn.onclick = (e) => { e.stopPropagation(); doRetranslate(row, seg); };
    zh.appendChild(btn);
  }

  const en = document.createElement("div");
  en.className = "cell col-en";
  en.innerHTML = `<div class="body">${seg.en_html}</div>`;
  en.title = "点击编辑英文（保存后写回 .tex 并回译中文）";
  en.onclick = () => startEditor(row, seg, "en");

  row.append(zh, en);
  return row;
}

function updateRow(row, seg, flashSel = ".col-en") {
  const fresh = buildRow(seg);
  row.replaceWith(fresh);
  renderMath(fresh);
  const flashCell = fresh.querySelector(flashSel);
  if (flashCell) {
    flashCell.classList.add("flash");
    setTimeout(() => flashCell.classList.remove("flash"), 1200);
  }
}

/* ---------- 编辑（中文/英文双向） ---------- */
function startEditor(row, seg, mode) {
  if (row.classList.contains("editing")) return;
  row.classList.add("editing");
  const isZh = mode === "zh";
  const cell = row.querySelector(isZh ? ".col-zh" : ".col-en");
  cell.innerHTML = "";
  const ta = document.createElement("textarea");
  ta.value = isZh ? (seg.zh || "") : seg.en;
  ta.placeholder = isZh
    ? "输入中文（LaTeX 命令/公式保持原样），Ctrl+Enter 保存，Esc 取消"
    : "编辑英文 LaTeX 源码，保存后写回 .tex 并回译中文，Ctrl+Enter 保存，Esc 取消";
  cell.appendChild(ta);
  const bar = document.createElement("div");
  bar.className = "editbar";
  const save = document.createElement("button");
  save.className = "primary";
  save.textContent = isZh ? "保存并翻译" : "保存并回译";
  const cancel = document.createElement("button");
  cancel.textContent = "取消";
  bar.append(save, cancel);
  cell.appendChild(bar);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  // 编辑框贴合内容高度（有下限，输入时自动长高）
  const fit = () => {
    ta.style.height = "auto";
    ta.style.height = Math.max(240, ta.scrollHeight + 8) + "px";
  };
  ta.addEventListener("input", fit);
  fit();

  const done = () => { row.classList.remove("editing"); };
  cancel.onclick = () => { done(); updateRow(row, seg); };
  ta.onkeydown = (e) => {
    if (e.isComposing) return;  // 中文输入法组词期间不响应快捷键
    if (e.key === "Escape") { done(); updateRow(row, seg); }
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) save.onclick();
  };
  save.onclick = async () => {
    const val = ta.value.trim();
    if (!val) { toast(isZh ? "中文不能为空" : "英文不能为空"); return; }
    save.disabled = true;
    save.textContent = isZh ? "翻译中…" : "回译中…";
    try {
      const d = await api(isZh ? "/api/edit" : "/api/edit-en", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(isZh
          ? { file: currentFile, idx: seg.idx, zh: val, provider }
          : { file: currentFile, idx: seg.idx, en: val, provider }),
      });
      done();
      updateRow(row, d.segment, isZh ? ".col-en" : ".col-zh");
      if (d.warnings && d.warnings.length) toast("⚠ " + d.warnings[0], 6000);
      else toast("✓ " + (isZh ? "已翻译并写回 " : "已写回并回译中文 ") + currentFile);
      refreshGitStatus();
    } catch (e) {
      toast("✗ " + e.message, 6000);
      save.disabled = false;
      save.textContent = isZh ? "保存并翻译" : "保存并回译";
    }
  };
}

function startEdit(row, seg) { startEditor(row, seg, "zh"); }

async function doRetranslate(row, seg) {
  const badge = row.querySelector(".badge");
  if (badge) { badge.textContent = "↻"; badge.className = "badge spin"; }
  try {
    const d = await api("/api/retranslate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: currentFile, idx: seg.idx, provider }),
    });
    updateRow(row, d.segment);
    toast("✓ 已翻译为中文");
  } catch (e) { toast("✗ " + e.message, 6000); loadSegments(currentFile); }
}

/* ---------- Git ---------- */
async function refreshGitStatus() {
  try {
    const s = await api("/api/git/status");
    const el = $("#gitstatus");
    const parts = [];
    if (s.dirty) parts.push(`${s.dirty} 未提交`);
    if (s.ahead) parts.push(`↑${s.ahead}`);
    if (s.behind) parts.push(`↓${s.behind}`);
    el.textContent = parts.length ? "● " + parts.join(" ") : "✓ 已同步";
    el.className = "git " + (parts.length ? "dirty" : "clean");
    el.title = `分支 ${s.branch}\n` + (s.dirty_files || []).join("\n");
  } catch (e) { /* 忽略 */ }
}

$("#btn-pull").onclick = async () => {
  try {
    const d = await api("/api/git/pull", { method: "POST" });
    showConsole(d.output || "(无输出)");
    if (d.ok) { toast("✓ 拉取完成"); await api("/api/refresh", { method: "POST" }); loadSegments(currentFile); }
    else toast("✗ 拉取失败，见输出", 6000);
    refreshGitStatus();
  } catch (e) { toast("✗ " + e.message, 6000); }
};

$("#btn-push").onclick = async () => {
  const message = prompt("提交信息：", "Update paper (bilingual edit)");
  if (message === null) return;
  try {
    const d = await api("/api/git/push", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    showConsole(d.output || "(无输出)");
    toast(d.ok ? "✓ 已推送到远端" : "✗ 推送失败，见输出", d.ok ? 3000 : 8000);
    refreshGitStatus();
  } catch (e) { toast("✗ " + e.message, 6000); }
};

$("#btn-refresh").onclick = async () => {
  await api("/api/refresh", { method: "POST" });
  await loadFiles();
  if (currentFile) loadSegments(currentFile);
  toast("✓ 已重新解析");
};

$("#btn-translate-file").onclick = async () => {
  if (!confirm(`把「${currentFile}」所有未翻译/过时的段落英→中？`)) return;
  toast("翻译中，请稍候…", 60000);
  try {
    const d = await api("/api/translate_file?file=" + encodeURIComponent(currentFile) +
                        "&provider=" + provider, { method: "POST" });
    toast(`✓ 完成 ${d.translated} 段` + (d.errors.length ? `，${d.errors.length} 段失败` : ""), 6000);
    if (d.errors.length) showConsole(d.errors.join("\n"));
    loadFiles(); loadSegments(currentFile);
  } catch (e) { toast("✗ " + e.message, 6000); }
};

/* ---------- Provider ---------- */
async function loadConfig() {
  const c = await api("/api/config");
  provider = c.active;
  const sel = $("#provider");
  sel.innerHTML = "";
  for (const [name, p] of Object.entries(c.providers)) {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = `${name} (${p.model})${p.has_key ? "" : " ⚠无key"}`;
    sel.appendChild(o);
  }
  sel.value = provider;
  sel.onchange = async () => {
    provider = sel.value;
    await api("/api/config?active=" + provider, { method: "POST" });
    toast("翻译后端 → " + provider);
  };
}

/* ---------- 启动 ---------- */
(async function init() {
  const st = await api("/api/status");
  if (!st.ready) { location.replace("/setup.html"); return; }
  await loadConfig();
  await loadFiles();
  // 支持 ?file=text/3-method.tex 深链
  const want = new URLSearchParams(location.search).get("file");
  if (want && FILES.some((f) => f.path === want)) loadSegments(want);
  refreshGitStatus();
  setInterval(refreshGitStatus, 30000);
})();
