r"""PaperSync 后端：段落浏览/编辑回写/翻译/章节对话/git 同步。

启动：python3 app.py（首次使用会进入设置向导，填写论文仓库 + LLM key）
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import render
import segmenter
import translator

BASE = Path(__file__).parent
FIG_CACHE = BASE / "cache" / "figures"

# 新用户设置向导的供应商默认值（api_key 留空，由用户在向导里填）
DEFAULT_PROVIDERS = {
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "api_key": "",
        "model": "kimi-k3",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "api_key": "",
        "model": "qwen-plus",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "",
        "model": "deepseek-chat",
    },
}

_edit_lock = asyncio.Lock()
_macros: dict[str, str] = {}


# ---------- 状态 ----------

class State:
    """zh_map.json 内存表示：files -> {rel: {"segments": [seg_dict]}}。按论文仓库隔离存储。"""

    def __init__(self, path: Path):
        self.path = path
        self.data = {"files": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def refresh_file(self, rel: str) -> list[dict]:
        """重解析 tex 文件并对齐已有中文，返回段落列表。"""
        content = (RT.repo / rel).read_text(encoding="utf-8")
        pf = segmenter.parse_tex(rel, content)
        new_texts = [s.text for s in pf.segments]
        old = self.data["files"].get(rel, {}).get("segments", [])
        old_texts = [s["en"] for s in old]
        mapping = segmenter.align_segments(old_texts, new_texts)

        segs = []
        for s in pf.segments:
            oi = mapping.get(s.idx)
            rec = {
                "idx": s.idx, "kind": s.kind, "env": s.env,
                "en": s.text, "en_hash": s.en_hash,
                "zh": "", "status": "untranslated", "warnings": [],
            }
            if oi is not None:
                o = old[oi]
                rec["zh"] = o.get("zh", "")
                if o.get("zh"):
                    if o.get("en_hash") == s.en_hash and o.get("status") != "warn":
                        rec["status"] = "synced"
                    elif o.get("en") == s.text:
                        rec["status"] = o.get("status", "synced")
                    else:
                        rec["status"] = "en_changed"  # 英文外部变了，中文过时
                    rec["warnings"] = o.get("warnings", [])
            segs.append(rec)

        self.data["files"][rel] = {"segments": segs}
        self.save()
        return segs

    def segs(self, rel: str) -> list[dict]:
        return self.data["files"][rel]["segments"]


# ---------- 运行时（惰性初始化：首次运行无配置也能启动设置页） ----------

class Runtime:
    def __init__(self):
        self.cfg: dict | None = None
        self.repo: Path | None = None
        self.slug: str = ""
        self.tex_files: list[str] = []
        self.state: State | None = None
        self.ready = False

    def init(self, cfg: dict) -> None:
        self.cfg = cfg
        self.repo = Path(cfg["repo_path"])
        self.slug = re.sub(r"[^\w.-]+", "-", self.repo.name) or "paper"
        # 迁移旧版单文件状态 → 按仓库隔离（旧文件保留作备份，不删除）
        legacy = BASE / "state" / "zh_map.json"
        new_path = BASE / "state" / self.slug / "zh_map.json"
        if legacy.exists() and not new_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, new_path)
        self.state = State(new_path)
        self.tex_files = discover_tex_files(self.repo)
        _refresh_macros()
        self.ready = True


RT = Runtime()


def _inputs_of(path: Path) -> list[str]:
    r"""非注释行里的 \input/\include 引用（按出现顺序）。_strip_comment 按单行处理。"""
    refs = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        for m in re.finditer(r"\\(?:input|include)\{([^}]+)\}",
                             segmenter._strip_comment(line)):
            refs.append(m.group(1).strip())
    return refs


def discover_tex_files(repo: Path) -> list[str]:
    r"""发现论文 tex 文件：main.tex + 非注释行 \input/\include 引用（递归、按序）+ 兜底扫描。"""
    files: list[str] = []
    main = repo / "main.tex"
    if main.exists():
        files.append("main.tex")
        queue: list[str] = []
        seen: set[str] = set()

        def push(raw: str):
            rel = raw if raw.endswith(".tex") else raw + ".tex"
            if rel not in seen:
                seen.add(rel)
                queue.append(rel)

        for ref in _inputs_of(main):
            push(ref)
        while queue:
            rel = queue.pop(0)
            p = repo / rel
            if not p.exists():
                continue
            if rel not in files:
                files.append(rel)
            for ref in _inputs_of(p):
                push(ref)
        return files
    # 无 main.tex：按目录扫描（排除构建/资源目录）
    skip_dirs = {".git", "figures", "build", "out", "archive",
                 "node_modules", ".venv", "venv", "cache"}
    out = []
    for p in sorted(repo.rglob("*.tex")):
        if any(part in skip_dirs for part in p.parts[len(repo.parts):]):
            continue
        out.append(p.relative_to(repo).as_posix())
    return out


def _refresh_macros():
    global _macros
    _macros = render.load_macros(RT.repo) if RT.repo else {}


def _require_ready():
    if not RT.ready:
        raise HTTPException(409, "未配置：请先完成设置")


def _macro_hint() -> str | None:
    names = sorted(_macros.keys())
    return ", ".join(names) if names else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cfg = translator.load_config(required=False)
    if cfg and Path(cfg.get("repo_path", "")).exists():
        RT.init(cfg)
        FIG_CACHE.mkdir(parents=True, exist_ok=True)
        for rel in RT.tex_files:
            if (RT.repo / rel).exists():
                RT.state.refresh_file(rel)
    else:
        print("PaperSync 未配置：首次使用请完成设置向导")
    yield


app = FastAPI(title="PaperSync 双语校稿", lifespan=lifespan)


# ---------- 段落 API ----------

def _seg_view(rec: dict) -> dict:
    out = {k: rec[k] for k in ("idx", "kind", "env", "en", "zh", "status", "warnings")}
    out["en_html"] = render.render_segment(rec["kind"], rec["env"], rec["en"], _macros)
    out["zh_html"] = (
        render.render_segment(rec["kind"], rec["env"], rec["zh"], _macros) if rec["zh"] else ""
    )
    return out


@app.get("/api/files")
def list_files():
    _require_ready()
    files = []
    for rel in RT.tex_files:
        if rel not in RT.state.data["files"]:
            continue
        segs = RT.state.segs(rel)
        n_un = sum(1 for s in segs if s["status"] in ("untranslated", "en_changed"))
        n_warn = sum(1 for s in segs if s["status"] == "warn")
        files.append({"path": rel, "n_segments": len(segs),
                      "n_pending": n_un, "n_warn": n_warn})
    return {"files": files, "repo": str(RT.repo)}


@app.get("/api/segments")
def get_segments(file: str = Query(...)):
    _require_ready()
    if file not in RT.state.data["files"]:
        raise HTTPException(404, "未知文件")
    return {"file": file, "segments": [_seg_view(s) for s in RT.state.segs(file)]}


class EditReq(BaseModel):
    file: str
    idx: int
    zh: str
    provider: str | None = None


@app.post("/api/edit")
async def edit_segment(req: EditReq):
    """改中文 → 自动译回英文 → 写回 .tex → 更新状态。"""
    _require_ready()
    async with _edit_lock:
        rel = req.file
        segs = RT.state.segs(rel)
        if req.idx >= len(segs):
            raise HTTPException(404, "段落不存在")
        rec = segs[req.idx]
        rec["zh"] = req.zh
        rec["status"] = "translating"

        try:
            new_en = await translator.translate(
                req.zh, "zh2en", RT.cfg, req.provider, macro_hint=_macro_hint())
        except Exception as e:  # noqa: BLE001
            rec["status"] = "warn"
            rec["warnings"] = [f"翻译失败: {e}"]
            RT.state.save()
            raise HTTPException(502, f"翻译失败: {e}")

        warnings = translator.validate_translation(rec["en"], new_en)

        # 写回 tex 文件
        content = (RT.repo / rel).read_text(encoding="utf-8")
        pf = segmenter.parse_tex(rel, content)
        if req.idx >= len(pf.segments):
            raise HTTPException(500, "文件结构已变化，请刷新")
        new_content = segmenter.replace_segment(content, pf.segments[req.idx], new_en)
        (RT.repo / rel).write_text(new_content, encoding="utf-8")

        # 重解析 + 对齐（此时 en_hash 更新）
        fresh = RT.state.refresh_file(rel)
        frec = fresh[min(req.idx, len(fresh) - 1)]
        frec["zh"] = req.zh
        frec["status"] = "warn" if warnings else "synced"
        frec["warnings"] = warnings
        RT.state.save()
        return {"ok": True, "segment": _seg_view(frec), "warnings": warnings}


class RetranslateReq(BaseModel):
    file: str
    idx: int
    provider: str | None = None


@app.post("/api/retranslate")
async def retranslate_segment(req: RetranslateReq):
    """英 → 中（用于英文外部更新/首次未译的段落）。"""
    _require_ready()
    async with _edit_lock:
        rel = req.file
        segs = RT.state.refresh_file(rel)
        if req.idx >= len(segs):
            raise HTTPException(404, "段落不存在")
        rec = segs[req.idx]
        try:
            zh = await translator.translate(rec["en"], "en2zh", RT.cfg, req.provider,
                                            macro_hint=_macro_hint())
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"翻译失败: {e}")
        rec["zh"] = zh
        rec["status"] = "synced"
        rec["warnings"] = []
        RT.state.save()
        return {"ok": True, "segment": _seg_view(rec)}


@app.post("/api/translate_file")
async def translate_file(file: str = Query(...), provider: str | None = None):
    """批量：把本文件所有 未翻译/英文已变 的段落做 英→中。"""
    _require_ready()
    async with _edit_lock:
        segs = RT.state.refresh_file(file)
        todo = [s for s in segs if s["status"] in ("untranslated", "en_changed")]
        sem = asyncio.Semaphore(4)
        errors = []

        async def one(rec):
            async with sem:
                try:
                    rec["zh"] = await translator.translate(
                        rec["en"], "en2zh", RT.cfg, provider, macro_hint=_macro_hint())
                    rec["status"] = "synced"
                    rec["warnings"] = []
                except Exception as e:  # noqa: BLE001
                    errors.append(f"段 {rec['idx']}: {e}")

        await asyncio.gather(*(one(s) for s in todo))
        RT.state.save()
        return {"ok": not errors, "translated": len(todo) - len(errors), "errors": errors}


@app.post("/api/refresh")
def refresh():
    _require_ready()
    _refresh_macros()
    for rel in RT.tex_files:
        if (RT.repo / rel).exists():
            RT.state.refresh_file(rel)
    return {"ok": True}


# ---------- 图片 ----------

@app.get("/api/figure")
def get_figure(path: str = Query(...)):
    """提供 figures/ 下的图片；PDF 自动转 PNG（带缓存）。"""
    _require_ready()
    src = (RT.repo / path).resolve()
    if not str(src).startswith(str(RT.repo.resolve())) or not src.exists():
        raise HTTPException(404, "图片不存在")
    if src.suffix.lower() != ".pdf":
        return FileResponse(src)
    key = hashlib.sha1(f"{path}:{src.stat().st_mtime}".encode()).hexdigest()[:12]
    out = FIG_CACHE / f"{key}.png"
    if not out.exists():
        subprocess.run(
            ["pdftoppm", "-png", "-r", "130", "-f", "1", "-l", "1",
             str(src), str(FIG_CACHE / key)],
            check=True, capture_output=True,
        )
        produced = FIG_CACHE / f"{key}-1.png"
        if produced.exists():
            produced.rename(out)
        elif not out.exists():
            raise HTTPException(500, "PDF 转 PNG 失败")
    return FileResponse(out)


# ---------- 配置 ----------

@app.get("/api/config")
def get_config():
    _require_ready()
    provs = {
        name: {"model": p["model"], "has_key": bool(p.get("api_key"))}
        for name, p in RT.cfg["providers"].items()
    }
    return {"active": RT.cfg["active_provider"], "providers": provs}


@app.post("/api/config")
def set_config(active: str = Query(...)):
    _require_ready()
    if active not in RT.cfg["providers"]:
        raise HTTPException(400, "未知 provider")
    translator.save_active_provider(active)
    RT.cfg["active_provider"] = active
    return {"ok": True, "active": active}


# ---------- Git ----------

def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def _git(*args: str) -> tuple[int, str]:
    cmd = ["git", "-C", str(RT.repo)]
    tok = (RT.cfg or {}).get("github_token", "")
    if tok:
        auth = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
        cmd += ["-c", f"http.extraheader=AUTHORIZATION: basic {auth}"]
    cmd += list(args)
    return _run(cmd)


@app.get("/api/git/status")
def git_status():
    _require_ready()
    _, porcelain = _git("status", "--porcelain")
    dirty = [ln for ln in porcelain.splitlines() if ln.strip()]
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    rc, counts = _git("rev-list", "--left-right", "--count", "@{u}...HEAD")
    ahead = behind = 0
    if rc == 0:
        behind, ahead = (int(x) for x in counts.split())
    return {"branch": branch, "dirty": len(dirty), "dirty_files": dirty[:10],
            "ahead": ahead, "behind": behind}


@app.post("/api/git/pull")
def git_pull():
    _require_ready()
    rc, out = _git("pull", "--ff-only")
    if rc == 0:
        for rel in RT.tex_files:
            if (RT.repo / rel).exists():
                RT.state.refresh_file(rel)
    return {"ok": rc == 0, "output": out}


class PushReq(BaseModel):
    message: str = "Update paper via PaperSync"


@app.post("/api/git/push")
def git_push(req: PushReq):
    _require_ready()
    paths = [f for f in RT.tex_files if (RT.repo / f).exists()]
    _git("add", *paths)
    # 只看已暂存改动（忽略未跟踪文件，不受 git 语言环境影响）
    rc_staged, _ = _git("diff", "--cached", "--quiet")
    if rc_staged != 0:
        rc, out = _git("commit", "-m", req.message)
        if rc != 0:
            return {"ok": False, "output": out}
    else:
        out = "没有本地改动。"
    rc, out2 = _git("push")
    return {"ok": rc == 0, "output": (out + "\n" + out2).strip()}


# ---------- 设置向导 ----------

@app.get("/api/status")
def api_status():
    return {
        "ready": RT.ready,
        "repo": str(RT.repo) if RT.repo else None,
        "slug": RT.slug or None,
        "port": (RT.cfg or {}).get("port", 8787),
        "active_provider": (RT.cfg or {}).get("active_provider"),
        "files": len(RT.tex_files),
    }


@app.get("/api/setup/defaults")
def setup_defaults():
    provs = {}
    for name, p in DEFAULT_PROVIDERS.items():
        key = p["api_key"] or translator._env_or_bashrc(p["api_key_env"])
        provs[name] = {"base_url": p["base_url"], "model": p["model"],
                       "api_key_env": p["api_key_env"], "api_key": key}
    return {"providers": provs, "active": "kimi"}


def _clone_error(out: str) -> str:
    if "Authentication failed" in out or "could not read Username" in out:
        return "认证失败：请检查 GitHub Token（需要 repo 权限），或确认仓库对你可见"
    if "not found" in out.lower() or "does not appear to be a git repository" in out:
        return "仓库不存在，或为私有仓库（私有仓库需要填 Token）"
    return out.strip() or "克隆失败"


class SetupReq(BaseModel):
    repo_url: str = ""
    repo_path: str = ""
    github_token: str = ""
    active_provider: str = "kimi"
    providers: dict = {}


@app.post("/api/setup")
async def api_setup(req: SetupReq):
    """设置：本地路径或 GitHub 链接二选一 → 写 config.json → 初始化运行时。"""
    async with _edit_lock:
        if bool(req.repo_url.strip()) == bool(req.repo_path.strip()):
            raise HTTPException(400, "请填写 GitHub 链接或本地路径（二选一）")

        token = req.github_token.strip()
        repo: Path
        if req.repo_path.strip():
            repo = Path(req.repo_path.strip()).expanduser().resolve()
            if not repo.is_dir():
                raise HTTPException(400, "本地路径不存在")
            if not any(repo.rglob("*.tex")):
                raise HTTPException(400, "该目录下没有找到 .tex 文件")
        else:
            url = req.repo_url.strip()
            name = url.rstrip("/").split("/")[-1].removesuffix(".git") or "paper"
            target = BASE / "papers" / name
            if target.exists():
                raise HTTPException(400, f"目录已存在：{target}（删除后可重新克隆）")
            if url.startswith("git@"):  # ssh 协议
                clone_url = url
            elif token:  # https + token：克隆后从 remote 中清掉 token
                m = re.match(r"^https://([\w.-]+)/(.+)$", url)
                if not m:
                    raise HTTPException(400, "GitHub 链接格式不对（需要 https:// 或 git@ 开头）")
                clone_url = f"https://{token}@{m.group(1)}"
            else:
                if not url.startswith("https://"):
                    raise HTTPException(400, "GitHub 链接格式不对（需要 https:// 或 git@ 开头）")
                clone_url = url
            rc, out = _run(["git", "clone", "--depth", "1", clone_url, str(target)])
            if rc != 0:
                raise HTTPException(400, _clone_error(out))
            if token:
                _run(["git", "-C", str(target), "remote", "set-url", "origin", url])
            repo = target

        cfg = {
            "repo_path": str(repo),
            "port": (translator.load_config(required=False) or {}).get("port", 8787),
            "active_provider": req.active_provider,
            "providers": {},
            "github_token": token,  # 仅存本地 config.json，供 pull/push 时注入认证头
        }
        for name, prov in DEFAULT_PROVIDERS.items():
            user = (req.providers or {}).get(name, {})
            cfg["providers"][name] = {
                "base_url": user.get("base_url") or prov["base_url"],
                "api_key_env": prov["api_key_env"],
                "api_key": (user.get("api_key") or prov["api_key"]
                            or translator._env_or_bashrc(prov["api_key_env"])),
                "model": user.get("model") or prov["model"],
            }
        if req.active_provider not in cfg["providers"]:
            raise HTTPException(400, f"未知 provider: {req.active_provider}")

        translator.CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        RT.init(cfg)
        for rel in RT.tex_files:
            if (RT.repo / rel).exists():
                RT.state.refresh_file(rel)
        return {"ok": True, "repo": str(RT.repo), "slug": RT.slug, "files": len(RT.tex_files)}


# ---------- 章节对话 ----------

import chapter_chat  # noqa: E402 —— 放在文件末尾导入，避免循环依赖
app.include_router(chapter_chat.router)


# ---------- 静态 ----------

app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = (translator.load_config(required=False) or {}).get("port", 8787)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
