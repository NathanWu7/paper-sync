/* PaperSync 章节对话前端：每章独立上下文与历史记录 */

let chatBusy = false;
let chatDraft = ""; // 切文件时保留未发送的输入

function chatMsg(role, text, meta) {
  const div = document.createElement("div");
  div.className = "chat-msg " + (role === "user" ? "user" : "assistant");
  const b = document.createElement("div");
  b.className = "chat-bubble";
  b.textContent = text || "（无回复内容）";
  div.appendChild(b);
  if (meta) {
    const m = document.createElement("div");
    m.className = "chat-meta";
    m.textContent = meta;
    div.appendChild(m);
  }
  return div;
}

async function loadChatHistory(file) {
  $("#chat-file").textContent = file;
  const box = $("#chat-msgs");
  const draft = $("#chat-input").value; // 保留正在输入的内容
  chatDraft = draft;
  try {
    const d = await api("/api/chat/history?file=" + encodeURIComponent(file));
    box.innerHTML = "";
    if (!d.messages.length) {
      const e = document.createElement("div");
      e.className = "chat-empty";
      e.textContent = "对本节提出修改要求，例如：把实验描述改得更严谨、压缩第一段、给贡献点加一句动机…";
      box.appendChild(e);
    }
    for (const m of d.messages) {
      let meta = null;
      if (m.role === "assistant" && m.edits && m.edits.length) {
        meta = "已修改 " + m.edits.length + " 段";
      }
      box.appendChild(chatMsg(m.role, m.content, meta));
    }
    box.scrollTop = box.scrollHeight;
  } catch (e) {
    toast("✗ 加载对话历史失败：" + e.message, 6000);
  }
  $("#chat-input").value = draft;
}

function setChatBusy(busy) {
  chatBusy = busy;
  const ta = $("#chat-input"), btn = $("#chat-send");
  ta.disabled = busy;
  btn.disabled = busy;
  btn.textContent = busy ? "修改中…" : "提出修改";
  if (!busy) ta.focus();
}

async function sendChat() {
  if (chatBusy || !currentFile) return;
  const input = $("#chat-input");
  const message = input.value.trim();
  if (!message) return;
  const box = $("#chat-msgs");
  const emptyEl = box.querySelector(".chat-empty");
  if (emptyEl) emptyEl.remove();
  box.appendChild(chatMsg("user", message));
  box.scrollTop = box.scrollHeight;
  input.value = "";
  setChatBusy(true);
  try {
    const d = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: currentFile, message, provider }),
    });
    let meta = null;
    if (d.changed > 0) meta = "已修改 " + d.changed + " 段" + (d.edits.length ? "" : "");
    else meta = "无需修改";
    box.appendChild(chatMsg("assistant", d.reply, meta));
    if (d.skipped && d.skipped.length) {
      const skipLine = "（" + d.skipped.length + " 处修改因保护校验被跳过）";
      const w = document.createElement("div");
      w.className = "chat-warn";
      w.textContent = skipLine;
      w.title = d.skipped.map((s) => `段 ${s.idx}: ${s.reason}`).join("\n");
      box.appendChild(w);
    }
    if (d.edits && d.edits.length) {
      for (const e of d.edits) {
        if (e.warnings && e.warnings.length) {
          toast("⚠ 段 " + e.idx + " 有校验警告，悬停状态标记查看", 6000);
          break;
        }
      }
    }
    box.scrollTop = box.scrollHeight;
    if (d.changed > 0) {
      loadSegments(currentFile); // 刷新双栏（不触碰对话面板）
      refreshGitStatus();
      toast("✓ 已修改 " + d.changed + " 段并写回 " + currentFile);
    } else {
      toast("✓ 已完成，无需修改");
    }
  } catch (e) {
    toast("✗ " + e.message, 8000);
    box.appendChild(chatMsg("assistant", "修改失败：" + e.message));
  }
  setChatBusy(false);
}

$("#chat-send").onclick = sendChat;
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.isComposing) return; // 中文输入法组词期间不响应
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
});
$("#chat-clear").onclick = async () => {
  if (!currentFile || !confirm("清空本章的对话记录？")) return;
  try {
    await api("/api/chat/clear?file=" + encodeURIComponent(currentFile), { method: "POST" });
    loadChatHistory(currentFile);
    toast("✓ 已清空本章对话");
  } catch (e) { toast("✗ " + e.message, 6000); }
};
