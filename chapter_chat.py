r"""章节对话修订：针对当前章节的 LLM 英文修订 + 中文反向同步。

- 上下文 = 当前文件（章节）的所有段落，不含其他章节
- LLM 直接修订英文（投稿质量优先），中文由 en→zh 反向同步
- LaTeX 保护校验：丢公式/引用的编辑重试一轮后仍违规 → 跳过该段（绝不写坏 LaTeX）
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

router = APIRouter()

HISTORY_LIMIT = 50      # 每个文件保留的对话条数
CTX_TRUNC = 2000        # 每段注入的上下文截断（字符）
HARD_PAT = re.compile(r"引用/标签键|数学式")

REVISE_SYS = r"""You are a LaTeX paper revision assistant. The user gives an instruction for revising ONE chapter of the paper.
You are shown the chapter's segments (idx + kind + LaTeX source, plus a Chinese translation for grounding).

Rules — follow strictly:
1. Revise ONLY the English LaTeX source per the instruction. Make the MINIMAL set of edits needed — do not rewrite segments the instruction does not require changing.
2. Never change LaTeX structure: commands and their brace arguments, math ($...$, \(...\), \[...\], equation/align/gather/multline environments), citation/reference/label keys (\citep, \citet, \cite, \ref, \eqref, \cref, \Cref, \label) must be preserved exactly.
3. Do not merge, split, add or remove segments. Return edits only for segments that must change. If the instruction needs no change, return an empty edits array.
4. Use precise academic English consistent with the rest of the paper.
5. Output STRICT JSON only, no prose, no code fences:
{"reasoning": "<Chinese summary of what changed and why>", "edits": [{"idx": <int>, "new_en": "<COMPLETE new LaTeX segment text>"}]}
6. new_en is the full replacement segment text, never a diff."""

_app_mod = None


def _A():
    """延迟导入 app。python3 app.py 以 __main__ 运行时，必须取 __main__ 里的实例，
    否则 import app 会得到第二个未初始化的模块副本。"""
    global _app_mod
    if _app_mod is None:
        import sys
        m = sys.modules.get("__main__")
        if m is not None and hasattr(m, "RT"):
            _app_mod = m
        else:
            import app as _app_mod
    return _app_mod


# ---------- 对话历史（按文件存 state/<slug>/chats/） ----------

def _chats_dir() -> Path:
    return _A().RT.state.path.parent / "chats"


def _history_path(file: str) -> Path:
    safe = re.sub(r"[^\w.-]+", "_", file)
    return _chats_dir() / f"{safe}.json"


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


# ---------- LLM 调用与解析 ----------

async def _revise(client, model: str, sys_prompt: str, user_msg: str) -> str:
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ],
    }
    try:
        kwargs["response_format"] = {"type": "json_object"}
        resp = await client.chat.completions.create(**kwargs)
    except Exception:  # noqa: BLE001 —— 部分后端不支持 json_object，降级纯文本
        kwargs.pop("response_format", None)
        resp = await client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content


def _parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层应为对象")
    return data


def _validate_edits(segs: list[dict], edits_raw) -> list[dict]:
    """校验 LLM 返回的编辑：idx 合法 + 保护串检查。返回带 hard/soft 分类的编辑列表。"""
    out = []
    if not isinstance(edits_raw, list):
        return out
    for item in edits_raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx"))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(segs):
            continue
        new_en = str(item.get("new_en") or "").strip()
        if not new_en:
            continue
        warns = translator.validate_translation(segs[idx]["en"], new_en)
        hard = [w for w in warns if HARD_PAT.search(w)]
        soft = [w for w in warns if w not in hard]
        out.append({"idx": idx, "new_en": new_en, "hard": hard, "soft": soft})
    return out


# ---------- 端点 ----------

class ChatReq(BaseModel):
    file: str
    message: str
    provider: str | None = None


@router.post("/api/chat")
async def chat(req: ChatReq):
    """章节对话：按指令修订当前章节英文，校验后写回 .tex，中文反向同步。"""
    A = _A()
    A._require_ready()
    async with A._edit_lock:
        file = req.file
        if file not in A.RT.state.data["files"]:
            raise HTTPException(404, "未知文件")
        segs = A.RT.state.refresh_file(file)
        client, model, _ = translator.get_client(A.RT.cfg, req.provider)
        hint = A._macro_hint()
        sys_prompt = REVISE_SYS + (
            f"\n\nKnown macros defined in this paper's preamble: {hint}" if hint else "")

        # 上下文：仅当前章节
        ctx = []
        for s in segs:
            ctx.append(f"[idx={s['idx']} kind={s['kind']}] en: {s['en'][:CTX_TRUNC]}\n"
                       f"zh: {(s['zh'] or '')[:CTX_TRUNC]}")
        hist = load_history(file)
        hist_txt = "\n".join(f"{m['role']}: {m['content']}" for m in hist[-8:]) or "(无)"
        user_msg = (f"Chapter file: {file}\n\n" + "\n\n".join(ctx)
                    + f"\n\nPrevious conversation:\n{hist_txt}\n\nInstruction: {req.message}")

        # 第一轮
        raw = await _revise(client, model, sys_prompt, user_msg)
        try:
            data = _parse_json(raw)
        except (ValueError, json.JSONDecodeError) as e:
            raw = await _revise(client, model, sys_prompt,
                                user_msg + f"\n\n[Error] 上次输出不是合法 JSON: {e}。只输出严格 JSON。")
            try:
                data = _parse_json(raw)
            except (ValueError, json.JSONDecodeError) as e2:
                raise HTTPException(502, f"模型输出无法解析: {e2}")

        edits = _validate_edits(segs, data.get("edits"))
        hard = [e for e in edits if e["hard"]]
        if hard:
            feedback = ("These edits violated LaTeX structure protection:\n"
                        + "\n".join(f"idx={e['idx']}: {'; '.join(e['hard'])}" for e in hard)
                        + "\nFix ONLY those edits (keep other edits unchanged). Output the full JSON again.")
            raw = await _revise(client, model, sys_prompt,
                                user_msg + f"\n\n[Correction request]\n{feedback}")
            try:
                data = _parse_json(raw)
                edits = _validate_edits(segs, data.get("edits"))
            except (ValueError, json.JSONDecodeError):
                pass  # 重试解析失败：沿用上一轮校验结果

        ok_edits = [e for e in edits if not e["hard"]]
        skipped = [{"idx": e["idx"], "reason": "; ".join(e["hard"])} for e in edits if e["hard"]]
        # 去重 + 过滤无变化
        seen: set[int] = set()
        final = []
        for e in ok_edits:
            if e["idx"] in seen:
                continue
            seen.add(e["idx"])
            if e["new_en"] != segs[e["idx"]]["en"]:
                final.append(e)

        written = []
        if final:
            content = (A.RT.repo / file).read_text(encoding="utf-8")
            pf = segmenter.parse_tex(file, content)
            # 按 idx 降序写回，保证原始偏移量有效
            for e in sorted(final, key=lambda x: -x["idx"]):
                if e["idx"] >= len(pf.segments):
                    skipped.append({"idx": e["idx"], "reason": "文件结构已变化"})
                    continue
                content = segmenter.replace_segment(content, pf.segments[e["idx"]], e["new_en"])
                written.append(e)
            (A.RT.repo / file).write_text(content, encoding="utf-8")

            # 中文反向同步（仅修改过的段落）
            fresh = A.RT.state.refresh_file(file)
            for e in written:
                rec = fresh[e["idx"]] if e["idx"] < len(fresh) else None
                if rec is None:
                    continue
                try:
                    rec["zh"] = await translator.translate(
                        rec["en"], "en2zh", A.RT.cfg, req.provider, macro_hint=hint)
                except Exception:  # noqa: BLE001 —— 回译失败不阻塞写回
                    rec["zh"] = ""
                    rec["warnings"] = [w for w in rec["warnings"]
                                       if "中文回译失败" not in w] + ["中文回译失败"]
                rec["status"] = "warn" if e["soft"] else "synced"
                rec["warnings"] = [w for w in rec["warnings"] if w not in e["soft"]] + e["soft"]
            A.RT.state.save()

        reasoning = str(data.get("reasoning") or "").strip()
        hist.append({"role": "user", "content": req.message, "ts": _now()})
        hist.append({"role": "assistant", "content": reasoning,
                     "edits": [e["idx"] for e in written], "ts": _now()})
        save_history(file, hist[-HISTORY_LIMIT:])

        return {"ok": True, "changed": len(written), "reply": reasoning,
                "edits": [{"idx": e["idx"], "warnings": e["soft"]} for e in written],
                "skipped": skipped}


@router.get("/api/chat/history")
def chat_history(file: str = Query(...)):
    _A()._require_ready()
    return {"messages": load_history(file)}


@router.post("/api/chat/clear")
def chat_clear(file: str = Query(...)):
    _A()._require_ready()
    p = _history_path(file)
    if p.exists():
        p.unlink()
    return {"ok": True}
