r"""双后端 LLM 翻译模块（Kimi / Qwen，OpenAI 兼容协议）+ LaTeX 保护串校验。

- 只译自然语言，原样保留 LaTeX 命令、数学式、引用键、自定义宏
- zh→en 后做保护串校验：对比新旧英文的数学段/命令/引用键，防止翻译丢结构
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path

from openai import AsyncOpenAI

CONFIG_PATH = Path(__file__).parent / "config.json"

SYS_PROMPT = r"""You are an expert academic translator for a LaTeX paper.
{direction}

Rules — follow strictly:
1. Translate ONLY natural-language text. Keep ALL LaTeX exactly as-is:
   - commands and their brace arguments structure: \command{{...}}, \begin{{...}}...\end{{...}}
   - math: $...$, \(...\), \[...\], equation/align environments — never translate math content
   - citation/reference keys: \citep{{...}}, \citet{{...}}, \ref{{...}}, \cref{{...}}, \Cref{{...}}, \label{{...}} — never touch the keys
   - custom macros: any \command you don't recognize must be preserved verbatim
   - URLs, file paths, \includegraphics paths
2. Lines starting with % are comments: keep them unchanged.
3. Keep the paragraph/line structure of the fragment.
4. Use precise academic {target_lang}; keep terminology consistent with the surrounding text.
5. Output ONLY the translated LaTeX fragment. No explanations, no code fences, no quotes."""

DIR_ZH2EN = "Translate the following LaTeX fragment from Chinese to English."
DIR_EN2ZH = "Translate the following LaTeX fragment from English to Chinese."


def _env_or_bashrc(var: str) -> str:
    """先查环境变量；没有则解析 ~/.bashrc 的 export 行（服务以非交互方式启动时兜底）。"""
    val = os.environ.get(var, "")
    if val:
        return val
    bashrc = Path.home() / ".bashrc"
    if bashrc.exists():
        m = re.search(rf"^\s*export\s+{re.escape(var)}=[\"']?([^\"'\n]+)", bashrc.read_text(), re.M)
        if m:
            return m.group(1).strip()
    return ""


def load_config(required: bool = True) -> dict | None:
    """读取 config.json；不存在时 required=False 返回 None（首次运行未配置时用）。"""
    if not CONFIG_PATH.exists():
        if required:
            raise RuntimeError("config.json 不存在：请先完成设置向导（./start.sh）")
        return None
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    # 环境变量可被 config 里的 api_key 直填覆盖
    for name, prov in cfg["providers"].items():
        if not prov.get("api_key"):
            env = prov.get("api_key_env", "")
            prov["api_key"] = _env_or_bashrc(env) if env else ""
    return cfg


def save_active_provider(name: str) -> None:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cfg["active_provider"] = name
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_client(cfg: dict, provider: str | None = None) -> tuple[AsyncOpenAI, str, str]:
    name = provider or cfg["active_provider"]
    prov = cfg["providers"][name]
    if not prov["api_key"]:
        raise RuntimeError(
            f"provider {name} 没有 API key：请设置环境变量 {prov.get('api_key_env')} 或在 config.json 里填 api_key"
        )
    return AsyncOpenAI(base_url=prov["base_url"], api_key=prov["api_key"]), prov["model"], name


async def translate(text: str, direction: str, cfg: dict, provider: str | None = None,
                    retries: int = 3, macro_hint: str | None = None) -> str:
    """direction: 'zh2en' | 'en2zh'。macro_hint 为论文自定义宏清单（可选，帮助模型识别）。"""
    client, model, _ = get_client(cfg, provider)
    dir_text = DIR_ZH2EN if direction == "zh2en" else DIR_EN2ZH
    target = "English" if direction == "zh2en" else "Chinese"
    sys = SYS_PROMPT.format(direction=dir_text, target_lang=target)
    if macro_hint:
        sys += f"\n\nKnown macros defined in this paper's preamble:\n{macro_hint}"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": text},
                ],
            )
            out = resp.choices[0].message.content.strip()
            # 去掉可能的代码围栏
            out = re.sub(r"^```(?:latex|tex)?\s*\n?", "", out)
            out = re.sub(r"\n?```\s*$", "", out)
            # 源片段没有空行时，折叠译文引入的空行（空行会改变 LaTeX 段落结构）
            if "\n\n" not in text.replace("\n\n ", ""):
                out = re.sub(r"\n\s*\n+", "\n", out)
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"翻译失败（{retries} 次重试后）: {last_err}")


# ---------- LaTeX 保护串校验 ----------

MATH_RES = [
    re.compile(r"\\\[(.*?)\\\]", re.S),
    re.compile(r"\\\((.*?)\\\)", re.S),
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\$(?:\\.|[^$\\])+\$", re.S),
    re.compile(r"\\begin\{(equation\*?|align\*?|aligned|gather\*?|multline\*?)\}(.*?)\\end\{\1\}", re.S),
]
CMD_RE = re.compile(r"\\([a-zA-Z]+)")
KEY_RE = re.compile(r"\\(?:citep|citet|cite|ref|eqref|cref|Cref|label)\{([^}]*)\}")


def protected_tokens(text: str) -> dict[str, Counter]:
    math = Counter()
    for rx in MATH_RES:
        for m in rx.finditer(text):
            math[re.sub(r"\s+", "", m.group(0))] += 1
    cmds = Counter(CMD_RE.findall(text))
    keys = Counter()
    for m in KEY_RE.finditer(text):
        for k in m.group(1).split(","):
            keys[k.strip()] += 1
    return {"math": math, "cmds": cmds, "keys": keys}


def validate_translation(old_en: str, new_en: str) -> list[str]:
    """zh→en 后校验新英文是否丢了旧英文的保护结构。返回警告列表（空=通过）。"""
    warns: list[str] = []
    old = protected_tokens(old_en)
    new = protected_tokens(new_en)
    missing_keys = old["keys"] - new["keys"]
    if missing_keys:
        warns.append(f"丢失引用/标签键: {', '.join(sorted(missing_keys))}")
    extra_keys = new["keys"] - old["keys"]
    if extra_keys:
        warns.append(f"多出引用/标签键: {', '.join(sorted(extra_keys))}")
    missing_math = old["math"] - new["math"]
    if missing_math:
        sample = list(missing_math)[:3]
        warns.append(f"数学式变化/丢失 {sum(missing_math.values())} 处: {sample}")
    # 命令计数允许 ±10% 噪声（翻译可能改 \emph→斜体词等），只对明显丢失报警
    missing_cmds = old["cmds"] - new["cmds"]
    important = {c: n for c, n in missing_cmds.items() if n >= 1 and c not in
                 {"emph", "textbf", "textit", "texttt", "paragraph", "noindent", "vspace", "hspace"}}
    if important:
        top = sorted(important.items(), key=lambda x: -x[1])[:5]
        warns.append(f"命令减少: {', '.join(f'{c}×{n}' for c, n in top)}")
    return warns


if __name__ == "__main__":
    # 实测：英译中 + 中译英 + 校验
    sample = (
        "Multi-task reinforcement learning (MTRL) offers a natural path because shared task "
        "structure can improve representation learning~\\citep{deramo2020sharing}, and \\method{} "
        "reallocates gradient budget toward $U_k = \\E[\\hat{A}_t]$."
    )
    cfg = load_config()

    async def main():
        zh = await translate(sample, "en2zh", cfg)
        print("EN→ZH:", zh)
        en = await translate(zh, "zh2en", cfg)
        print("ZH→EN:", en)
        warns = validate_translation(sample, en)
        print("校验:", "通过" if not warns else warns)

    asyncio.run(main())
