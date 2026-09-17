r"""公式校对（实验性）：数值代入、向量/矩阵示例、一键可视化、符号一致性分析。

- 只分析不改 tex；LLM 建议的公式修正经 /api/formula/apply 单独应用
- 公式从英文段落提取（英文是唯一真实源）
- 对话历史按文件存 state/<slug>/formula/
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import segmenter
import translator
from chapter_chat import _parse_json, _revise

router = APIRouter()

ANALYZE_SYS = r"""You are a careful formula proofreader for a LaTeX paper. The author feels the formulas are too casual and wants human-level scrutiny.
{mode}

Context format: segments with idx, kind, a text snippet, and the LaTeX formulas inside.

Rules — follow strictly:
1. Work ONLY with the formulas given. Do not invent formulas.
2. The "formula" field must be the EXACT LaTeX string as given (copy it character by character).
3. All analysis in Chinese.
4. If you find real problems (dimension mismatch, undefined symbols, sloppy notation, wrong indexing), list them explicitly in "issue" — do not be polite about it.
5. Output STRICT JSON only, no prose, no code fences."""

MODE_NUMERIC = """For EACH formula, produce:
- meaning: what the formula computes; what each quantity means (with units when applicable).
- example: a concrete numeric instantiation. Choose plausible example values for the inputs and compute the result step by step with actual numbers. If the formula involves vectors or matrices, construct a small concrete example yourself (e.g. 2-dim vectors or 2x2 matrices) and carry the computation through — do not ask the user for values.
- issue: any error, sloppiness, or ambiguity found; empty string "" if none.
Output: {"results": [{"idx": <int>, "formula": "<exact>", "meaning": "...", "example": "...", "issue": "..."}]}"""

MODE_VECTOR = """Only for formulas that involve vectors or matrices: construct a small concrete example yourself (e.g. 2-dim vectors or 2x2 matrices with explicit numbers), compute the result step by step, and explain the geometric or algebraic meaning. Skip scalar-only formulas (do not include them in results).
Output: {"results": [{"idx": <int>, "formula": "<exact>", "example": "...", "issue": "..."}]}"""

MODE_VISUAL = """For EACH formula, produce its meaning and a standalone SVG visualization (a sketch of the geometry, a small plot of the function, a diagram of the computation flow — whatever best conveys the formula's meaning). SVG rules: viewBox "0 0 460 300"; clean minimal design; background #FBFBF8; ink #21241F; accent #B53A2B; grid/axis lines #D8DBD5; English labels only; no scripts, no external references, no text outside the viewBox; keep labels short.
Output: {"results": [{"idx": <int>, "formula": "<exact>", "meaning": "...", "svg": "<svg ...>...</svg>"}]}"""

MODE_CONSISTENCY = """Below are ALL formulas in the paper (file + segment idx). Some quantity symbols appear as bare LaTeX macros without math delimiters (e.g. \\mathcal{B} in running text) — treat them as symbols too. Find groups of formulas that express the SAME mathematical meaning but use INCONSISTENT symbols or notation (e.g. the same quantity written as U_k in one place and U in another; $B$ vs \\mathcal{B}; the same operation denoted differently). For each group: name the quantity, list the exact formula strings that conflict, explain the inconsistency, and propose ONE unified notation. Only report real conflicts; if none, return an empty groups array and say so in summary.
Output: {"groups": [{"name": "...", "formulas": ["<exact 1>", "<exact 2>"], "issue": "...", "suggestion": "..."}], "summary": "..."}"""

CHAT_SYS = r"""You are a formula consultant for a LaTeX paper. Below are the formulas in the current chapter (with segment idx).
Answer the user's question about these formulas in Chinese, precisely and concretely. If the user asks to fix or improve a formula, propose corrections as fixes.
Output STRICT JSON: {"reply": "...", "fixes": [{"idx": <int>, "old": "<exact formula LaTeX as given>", "new": "<corrected formula LaTeX>", "note": "..."}]}
fixes is empty when no correction is proposed. The "old" string must be copied EXACTLY from the given formulas."""

_app_mod = None


def _A():
    """延迟导入 app（app.py 在文件末尾导入本模块；python3 app.py 以 __main__ 运行时取 __main__）。"""
    global _app_mod
    if _app_mod is None:
        import sys
        m = sys.modules.get("__main__")
        if m is not None and hasattr(m, "RT"):
            _app_mod = m
        else:
            import app as _app_mod
    return _app_mod


# ---------- 公式提取 ----------

# 不带 $ 定界符的裸数学宏（如 \mathcal{B}、\mathbf{w}）—— 只用于全篇一致性扫描
BARE_RE = re.compile(r"\\(?:mathcal|mathbf|mathbb|mathit|mathrm|bm)\{[^}]*\}")


def extract_formulas(text: str) -> list[str]:
    """提取段落中的公式（按出现顺序，过滤嵌套重复与单符号）。"""
    spans = []
    for rx in translator.MATH_RES:
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    spans.sort()
    out: list[str] = []
    last_end = -1
    for s, e in spans:
        if s < last_end:
            continue
        f = text[s:e].strip()
        if len(f) >= 4 and f not in out:
            out.append(f)
        last_end = e
    return out


def chapter_formulas(file: str) -> list[dict]:
    A = _A()
    rows = []
    for s in A.RT.state.segs(file):
        fs = extract_formulas(s["en"])
        if fs:
            rows.append({"idx": s["idx"], "kind": s["kind"],
                         "snippet": s["en"][:160], "formulas": fs})
    return rows


def paper_formulas(limit: int = 150) -> list[dict]:
    A = _A()
    items = []
    for rel in A.RT.tex_files:
        for s in A.RT.state.segs(rel):
            seen = set(extract_formulas(s["en"]))
            # 裸数学宏也纳入一致性扫描（\mathcal{B} 与 B 这类冲突常以裸宏形式出现）
            for m in BARE_RE.finditer(s["en"]):
                f = m.group(0).strip()
                if len(f) >= 4 and f not in seen:
                    seen.add(f)
            for f in seen:
                if len(f) <= 300:
                    items.append({"file": rel, "idx": s["idx"], "formula": f})
        if len(items) >= limit:
            break
    return items[:limit]


# ---------- 历史 ----------

def _history_dir() -> Path:
    return _A().RT.state.path.parent / "formula"


def _history_path(file: str) -> Path:
    import re
    safe = re.sub(r"[^\w.-]+", "_", file)
    return _history_dir() / f"{safe}.json"


def load_history(file: str) -> list[dict]:
    p = _history_path(file)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("messages", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_history(file: str, messages: list[dict]) -> None:
    p = _history_path(file)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"messages": messages}, ensure_ascii=False, indent=1),
                 encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


async def _ask_json(client, model: str, sys_prompt: str, user_msg: str) -> dict:
    raw = await _revise(client, model, sys_prompt, user_msg)
    try:
        return _parse_json(raw)
    except ValueError as e:
        raw = await _revise(client, model, sys_prompt,
                            user_msg + f"\n\n[Error] 上次输出不是合法 JSON: {e}。只输出严格 JSON。")
        try:
            return _parse_json(raw)
        except ValueError as e2:
            raise HTTPException(502, f"模型输出无法解析: {e2}")


# ---------- 端点 ----------

class AnalyzeReq(BaseModel):
    file: str
    mode: str = "numeric"  # numeric | vector | visualize | consistency
    provider: str | None = None


@router.post("/api/formula/analyze")
async def formula_analyze(req: AnalyzeReq):
    """公式分析：数值代入 / 向量矩阵示例 / 可视化 / 全篇符号一致性。只读，不改 tex。"""
    A = _A()
    A._require_ready()
    client, model, _ = translator.get_client(A.RT.cfg, req.provider)

    if req.mode == "consistency":
        items = paper_formulas()
        if not items:
            return {"ok": True, "mode": req.mode, "reply": "论文里没有找到公式。",
                    "results": [], "groups": []}
        ctx = "\n".join(f"[{i['file']}#{i['idx']}] {i['formula']}" for i in items)
        data = await _ask_json(client, model, ANALYZE_SYS.format(mode=MODE_CONSISTENCY),
                               f"Paper formulas ({len(items)}):\n{ctx}")
        reply = str(data.get("summary") or "").strip()
        msg = {"role": "assistant", "content": reply,
               "data": {"mode": req.mode, "groups": data.get("groups") or []}, "ts": _now()}
        hist = load_history(req.file)
        save_history(req.file, (hist + [msg])[-100:])
        return {"ok": True, "mode": req.mode, "reply": reply,
                "results": [], "groups": data.get("groups") or [], "total": len(items)}

    if req.mode not in ("numeric", "vector", "visualize"):
        raise HTTPException(400, "未知分析模式")
    if req.file not in A.RT.state.data["files"]:
        raise HTTPException(404, "未知文件")
    rows = chapter_formulas(req.file)
    if req.mode == "visualize":
        rows = rows[:25]  # SVG 体积大，限制条数
    if not rows:
        return {"ok": True, "mode": req.mode, "reply": "本章没有找到公式。",
                "results": [], "groups": []}
    ctx = "\n\n".join(
        f"[idx={r['idx']} kind={r['kind']}] snippet: {r['snippet']}\n"
        f"formulas: {json.dumps(r['formulas'], ensure_ascii=False)}" for r in rows)
    mode_p = {"numeric": MODE_NUMERIC, "vector": MODE_VECTOR, "visualize": MODE_VISUAL}[req.mode]
    data = await _ask_json(client, model, ANALYZE_SYS.format(mode=mode_p),
                           f"Chapter file: {req.file}\n\n{ctx}")
    results = data.get("results") or []
    reply = f"已分析 {len(results)} 条公式"
    msg = {"role": "assistant", "content": reply,
           "data": {"mode": req.mode, "results": results}, "ts": _now()}
    hist = load_history(req.file)
    save_history(req.file, (hist + [msg])[-100:])
    return {"ok": True, "mode": req.mode, "reply": reply, "results": results, "groups": []}


class FormulaChatReq(BaseModel):
    file: str
    message: str
    provider: str | None = None


@router.post("/api/formula/chat")
async def formula_chat(req: FormulaChatReq):
    """公式专项对话：上下文仅当前章节的公式。可返回建议修正(不自动应用)。"""
    A = _A()
    A._require_ready()
    if req.file not in A.RT.state.data["files"]:
        raise HTTPException(404, "未知文件")
    rows = chapter_formulas(req.file)
    if not rows:
        return {"ok": True, "reply": "本章没有找到公式。", "fixes": []}
    client, model, _ = translator.get_client(A.RT.cfg, req.provider)
    ctx = "\n\n".join(
        f"[idx={r['idx']} kind={r['kind']}] snippet: {r['snippet']}\n"
        f"formulas: {json.dumps(r['formulas'], ensure_ascii=False)}" for r in rows)
    hist = load_history(req.file)
    hist_txt = "\n".join(f"{m['role']}: {m['content']}" for m in hist[-8:]) or "(无)"
    user_msg = (f"Chapter file: {req.file}\n\n{ctx}\n\n"
                f"Previous conversation:\n{hist_txt}\n\nQuestion: {req.message}")
    data = await _ask_json(client, model, CHAT_SYS, user_msg)
    reply = str(data.get("reply") or "").strip()
    fixes = data.get("fixes") or []
    hist.append({"role": "user", "content": req.message, "ts": _now()})
    hist.append({"role": "assistant", "content": reply, "data": {"fixes": fixes}, "ts": _now()})
    save_history(req.file, hist[-100:])
    return {"ok": True, "reply": reply, "fixes": fixes}


class ApplyReq(BaseModel):
    file: str
    idx: int
    old: str
    new: str
    provider: str | None = None


def _check_formula(new: str) -> list[str]:
    warns = []
    if new.count("$") % 2:
        warns.append("美元符号 $ 不配对")
    if new.count("\\[") != new.count("\\]"):
        warns.append("\\[ \\] 不配对")
    if new.count("\\begin{") != new.count("\\end{"):
        warns.append("环境 begin/end 不配对")
    if translator.KEY_RE.search(new):
        warns.append("公式内出现引用键，不允许")
    return warns


@router.post("/api/formula/apply")
async def formula_apply(req: ApplyReq):
    """应用公式修正：在段内做精确字符串替换 → 写回 .tex → 中文反向同步。"""
    A = _A()
    A._require_ready()
    async with A._edit_lock:
        rel = req.file
        segs = A.RT.state.refresh_file(rel)
        if req.idx >= len(segs):
            raise HTTPException(404, "段落不存在")
        rec = segs[req.idx]
        old, new = req.old.strip(), req.new.strip()
        if not old or not new or old == new:
            raise HTTPException(400, "新旧公式不能为空且不能相同")
        if rec["en"].count(old) != 1:
            raise HTTPException(400, "原文中找不到该公式（或出现多次），请刷新后重试")
        warns = _check_formula(new)
        new_en = rec["en"].replace(old, new, 1)

        content = (A.RT.repo / rel).read_text(encoding="utf-8")
        pf = segmenter.parse_tex(rel, content)
        if req.idx >= len(pf.segments):
            raise HTTPException(500, "文件结构已变化，请刷新")
        new_content = segmenter.replace_segment(content, pf.segments[req.idx], new_en)
        (A.RT.repo / rel).write_text(new_content, encoding="utf-8")

        fresh = A.RT.state.refresh_file(rel)
        frec = fresh[min(req.idx, len(fresh) - 1)]
        zh_warn = ""
        try:
            frec["zh"] = await translator.translate(
                frec["en"], "en2zh", A.RT.cfg, req.provider, macro_hint=A._macro_hint())
        except Exception as e:  # noqa: BLE001
            frec["zh"] = ""
            zh_warn = f"中文回译失败: {e}"
        frec["status"] = "warn" if (warns or zh_warn) else "synced"
        frec["warnings"] = warns + ([zh_warn] if zh_warn else [])
        A.RT.state.save()
        return {"ok": True, "segment": A._seg_view(frec), "warnings": frec["warnings"]}


@router.get("/api/formula/history")
def formula_history(file: str = Query(...)):
    _A()._require_ready()
    return {"messages": load_history(file)}


@router.post("/api/formula/clear")
def formula_clear(file: str = Query(...)):
    _A()._require_ready()
    p = _history_path(file)
    if p.exists():
        p.unlink()
    return {"ok": True}
