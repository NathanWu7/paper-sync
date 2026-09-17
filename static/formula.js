/* PaperSync 公式校对前端：数值代入 / 向量矩阵示例 / 可视化 / 符号一致性 + 专项对话 */

let formulaBusy = false;

function sanitizeSvg(svg) {
  return String(svg)
    .replace(/<script[\s\S]*?<\/script>/gi, "")
    .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
    .replace(/(href|xlink:href)\s*=\s*(['"])\s*javascript:[^'"]*\2/gi, "");
}

function fFormula(tex) {
  const span = document.createElement("span");
  span.className = "math";
  span.textContent = tex;
  return span;
}

/* 分析结果卡片 */
function fResultCard(r, mode) {
  const card = document.createElement("div");
  card.className = "f-card";
  const head = document.createElement("div");
  head.className = "f-formula";
  head.appendChild(fFormula(r.formula));
  card.appendChild(head);
  if (r.meaning) {
    const p = document.createElement("div");
    p.className = "f-meaning";
    p.textContent = r.meaning;
    card.appendChild(p);
  }
  if (r.example) {
    const p = document.createElement("div");
    p.className = "f-example";
    p.textContent = r.example;
    card.appendChild(p);
  }
  if (r.issue) {
    const p = document.createElement("div");
    p.className = "f-issue";
    p.textContent = "⚠ " + r.issue;
    card.appendChild(p);
  }
  if (r.svg) {
    const box = document.createElement("div");
    box.className = "f-svg";
    box.innerHTML = sanitizeSvg(r.svg);
    card.appendChild(box);
  }
  const tag = document.createElement("span");
  tag.className = "f-tag";
  tag.textContent = "段 " + r.idx;
  card.appendChild(tag);
  return card;
}

/* 一致性分组卡片 */
function fGroupCard(g) {
  const card = document.createElement("div");
  card.className = "f-card f-group";
  const name = document.createElement("div");
  name.className = "f-group-name";
  name.textContent = g.name || "未命名量";
  card.appendChild(name);
  const list = document.createElement("div");
  list.className = "f-group-formulas";
  for (const f of (g.formulas || [])) list.appendChild(fFormula(f));
  card.appendChild(list);
  if (g.issue) {
    const p = document.createElement("div");
    p.className = "f-issue";
    p.textContent = "⚠ " + g.issue;
    card.appendChild(p);
  }
  if (g.suggestion) {
    const p = document.createElement("div");
    p.className = "f-suggestion";
    p.textContent = "建议：" + g.suggestion;
    card.appendChild(p);
  }
  return card;
}

/* 建议修正卡片(带应用按钮) */
function fFixCard(fix) {
  const card = document.createElement("div");
  card.className = "f-card f-fix";
  const row = document.createElement("div");
  row.className = "f-fix-row";
  const oldEl = document.createElement("span");
  oldEl.className = "f-fix-old";
  oldEl.appendChild(fFormula(fix.old));
  const arrow = document.createElement("span");
  arrow.className = "f-fix-arrow";
  arrow.textContent = "→";
  const newEl = document.createElement("span");
  newEl.className = "f-fix-new";
  newEl.appendChild(fFormula(fix.new));
  row.append(oldEl, arrow, newEl);
  card.appendChild(row);
  if (fix.note) {
    const p = document.createElement("div");
    p.className = "f-meaning";
    p.textContent = fix.note;
    card.appendChild(p);
  }
  const bar = document.createElement("div");
  bar.className = "f-fix-bar";
  const tag = document.createElement("span");
  tag.className = "f-tag";
  tag.textContent = "段 " + fix.idx;
  const btn = document.createElement("button");
  btn.className = "f-apply";
  btn.textContent = "应用此修正";
  btn.title = "写回 .tex 并回译中文";
  btn.onclick = async () => {
    if (formulaBusy) return;
    btn.disabled = true;
    btn.textContent = "应用中…";
    try {
      const d = await api("/api/formula/apply", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: currentFile, idx: fix.idx, old: fix.old, new: fix.new, provider }),
      });
      toast(d.warnings && d.warnings.length ? "⚠ " + d.warnings[0] : "✓ 已应用公式修正");
      loadSegments(currentFile);
      refreshGitStatus();
      card.classList.add("f-fix-applied");
      btn.textContent = "已应用";
    } catch (e) {
      toast("✗ " + e.message, 6000);
      btn.disabled = false;
      btn.textContent = "应用此修正";
    }
  };
  bar.append(tag, btn);
  card.appendChild(bar);
  return card;
}

function fMsgBubble(role, text) {
  const div = document.createElement("div");
  div.className = "chat-msg " + (role === "user" ? "user" : "assistant");
  const b = document.createElement("div");
  b.className = "chat-bubble";
  b.textContent = text || "（无内容）";
  div.appendChild(b);
  return div;
}

async function loadFormulaHistory(file) {
  $("#formula-file").textContent = file;
  const box = $("#formula-msgs");
  const draft = $("#formula-input").value;
  try {
    const d = await api("/api/formula/history?file=" + encodeURIComponent(file));
    box.innerHTML = "";
    if (!d.messages.length) {
      const e = document.createElement("div");
      e.className = "chat-empty";
      e.textContent = "针对公式单独提问，例如「这个上界公式的推导是否成立？」「把 $\alpha_t$ 的记号统一」…";
      box.appendChild(e);
    }
    for (const m of d.messages) {
      if (m.role === "user") { box.appendChild(fMsgBubble("user", m.content)); continue; }
      if (m.data && m.data.mode === "consistency") {
        box.appendChild(fMsgBubble("assistant", m.content || "符号一致性分析"));
        for (const g of (m.data.groups || [])) box.appendChild(fGroupCard(g));
        if (!(m.data.groups || []).length) box.appendChild(fMsgBubble("assistant", "未发现符号不一致。"));
      } else if (m.data && m.data.results) {
        box.appendChild(fMsgBubble("assistant", m.content));
        for (const r of m.data.results) box.appendChild(fResultCard(r, m.data.mode));
      } else if (m.data && m.data.fixes) {
        box.appendChild(fMsgBubble("assistant", m.content));
        for (const f of m.data.fixes) box.appendChild(fFixCard(f));
      } else {
        box.appendChild(fMsgBubble("assistant", m.content));
      }
    }
    box.scrollTop = box.scrollHeight;
    renderMath(box);
  } catch (e) {
    toast("✗ 加载公式对话失败：" + e.message, 6000);
  }
  $("#formula-input").value = draft;
}

function setFormulaBusy(busy) {
  formulaBusy = busy;
  for (const b of document.querySelectorAll("#formula button")) b.disabled = busy;
  $("#formula-input").disabled = busy;
}

async function runAnalyze(mode, label) {
  if (formulaBusy || !currentFile) return;
  const box = $("#formula-msgs");
  const emptyEl = box.querySelector(".chat-empty");
  if (emptyEl) emptyEl.remove();
  const note = fMsgBubble("user", label + "（" + (mode === "consistency" ? "全篇" : "本章") + "）");
  box.appendChild(note);
  setFormulaBusy(true);
  toast(label + " 分析中，请稍候…", 60000);
  try {
    const d = await api("/api/formula/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: currentFile, mode, provider }),
    });
    box.appendChild(fMsgBubble("assistant", d.reply));
    if (mode === "consistency") {
      for (const g of (d.groups || [])) box.appendChild(fGroupCard(g));
      if (!(d.groups || []).length) box.appendChild(fMsgBubble("assistant", "未发现符号不一致。"));
    } else {
      for (const r of (d.results || [])) box.appendChild(fResultCard(r, mode));
    }
    box.scrollTop = box.scrollHeight;
    renderMath(box);
    toast("✓ " + d.reply);
  } catch (e) {
    toast("✗ " + e.message, 8000);
    box.appendChild(fMsgBubble("assistant", "分析失败：" + e.message));
  }
  setFormulaBusy(false);
}

async function sendFormulaChat() {
  if (formulaBusy || !currentFile) return;
  const input = $("#formula-input");
  const message = input.value.trim();
  if (!message) return;
  const box = $("#formula-msgs");
  const emptyEl = box.querySelector(".chat-empty");
  if (emptyEl) emptyEl.remove();
  box.appendChild(fMsgBubble("user", message));
  input.value = "";
  setFormulaBusy(true);
  try {
    const d = await api("/api/formula/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: currentFile, message, provider }),
    });
    box.appendChild(fMsgBubble("assistant", d.reply));
    for (const f of (d.fixes || [])) box.appendChild(fFixCard(f));
    box.scrollTop = box.scrollHeight;
    renderMath(box);
  } catch (e) {
    toast("✗ " + e.message, 8000);
    box.appendChild(fMsgBubble("assistant", "失败：" + e.message));
  }
  setFormulaBusy(false);
}

$("#f-numeric").onclick = () => runAnalyze("numeric", "数值代入分析");
$("#f-vector").onclick = () => runAnalyze("vector", "向量/矩阵示例");
$("#f-visual").onclick = () => runAnalyze("visualize", "公式可视化");
$("#f-consistency").onclick = () => runAnalyze("consistency", "符号一致性分析");
$("#formula-send").onclick = sendFormulaChat;
$("#formula-input").addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendFormulaChat();
  }
});
$("#formula-clear").onclick = async () => {
  if (!currentFile || !confirm("清空本章的公式对话记录？")) return;
  try {
    await api("/api/formula/clear?file=" + encodeURIComponent(currentFile), { method: "POST" });
    loadFormulaHistory(currentFile);
    toast("✓ 已清空本章公式对话");
  } catch (e) { toast("✗ " + e.message, 6000); }
};
