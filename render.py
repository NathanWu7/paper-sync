r"""LaTeX 片段 → 展示用 HTML。

策略：后端做轻量转换（宏展开、常用命令转标签、图片/公式抽取），
数学部分保留原始 tex 放在 <span/div class="math"> 里，由前端 KaTeX 渲染。
不支持完美排版，目标是"读得顺 + 认得全"。
"""

from __future__ import annotations

import html
import re
from pathlib import Path

# ---------- 宏展开 ----------

def load_macros(repo: Path) -> dict[str, str]:
    """从 main.tex 解析无参数 \newcommand（数学类宏保留原样，文本类展开）。"""
    macros: dict[str, str] = {}
    src = (repo / "main.tex").read_text(encoding="utf-8")
    for m in re.finditer(r"\\newcommand\{\\(\w+)\}(?:\[(\d+)\])?\{((?:[^{}]|\{[^{}]*\})*)\}", src):
        name, nargs, body = m.group(1), m.group(2), m.group(3)
        if nargs:  # 带参数的不展开（\placeholder 等单独处理）
            continue
        macros[name] = body
    return macros


# 数学类宏：在数学模式里直接保留原名（KaTeX 不认识但很少出现在要渲染的展示文本里，
# 这里把常见映射直接展开成 KaTeX 认识的式子）
MATH_MACRO_EXPANSIONS = {
    "R": r"\mathbb{R}", "E": r"\mathbb{E}", "D": r"\mathcal{D}", "T": r"\mathcal{T}",
    "M": r"\mathcal{M}", "A": r"\mathcal{A}", "Sspace": r"\mathcal{S}",
    "G": r"\mathcal{G}", "obs": r"\mathcal{O}",
    # KaTeX 不支持 \bm，用 \boldsymbol 渲染
    "btheta": r"\boldsymbol{\theta}", "ba": r"\boldsymbol{a}", "bs": r"\boldsymbol{s}",
    "bo": r"\boldsymbol{o}", "bw": r"\boldsymbol{w}",
}


def expand_text_macros(tex: str, macros: dict[str, str], depth: int = 3) -> str:
    r"""展开文本区的无参宏（\method → DGPO 等）。"""
    for _ in range(depth):
        changed = False
        for name, body in macros.items():
            if name in MATH_MACRO_EXPANSIONS:
                continue  # 数学宏在数学模式里处理
            pat = re.compile(r"\\" + name + r"\{\}?")
            new = pat.sub(lambda _m, b=body: b, tex)
            if new != tex:
                tex, changed = new, True
        if not changed:
            break
    return tex


# ---------- 数学抽取 ----------

MATH_BLOCK_RES = [
    re.compile(r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?)\}(.*?)\\end\{\1\}", re.S),
    re.compile(r"\\\[(.*?)\\\]", re.S),
]
MATH_INLINE_RE = re.compile(r"(\$(?:\\.|[^$\\])+\$|\\\((?:\\.|[^\\])*?\\\))")


def _expand_math_macros(tex: str) -> str:
    for name, body in MATH_MACRO_EXPANSIONS.items():
        tex = re.sub(r"\\" + name + r"(?![a-zA-Z])", lambda _m, b=body: b, tex)
    return tex


def _math_span(raw: str, display: bool) -> str:
    inner = raw
    inner = re.sub(r"^\\\(|\\\)$|^\$|\$$|^\\\[|\\\]$", "", inner)
    # KaTeX 不支持 \label/\nonumber/\intertext，渲染前剥离
    inner = re.sub(r"\\label\{[^}]*\}|\\nonumber\b", "", inner)
    env_m = re.match(r"\\begin\{(\w+\*?)\}(.*)\\end\{\w+\*?\}", inner, re.S)  # 贪婪匹配到最外层 \end
    if env_m:
        env, body = env_m.group(1), env_m.group(2)
        if env in ("equation", "equation*", "align", "align*", "gather", "gather*"):
            # 内容已含对齐环境时不再外包 aligned（避免 KaTeX 嵌套问题）
            if not re.search(r"\\begin\{(aligned|split|cases|array)\}", body):
                inner = r"\begin{aligned}" + body + r"\end{aligned}"
            else:
                inner = body
    cls = "math display" if display else "math"
    return f'<span class="{cls}">{html.escape(_expand_math_macros(inner.strip()))}</span>'


# ---------- 常用命令 ----------

def _simple_cmds(tex: str) -> str:
    """把常见格式命令转成 HTML 标签。在数学抽取之后调用（此时剩余的都是文本）。"""
    def braced(cmd: str, tex: str, repl) -> str:
        # 简单一层花括号匹配（正文里足够用）；cmd 支持 a|b 交替
        return re.sub(r"\\(?:" + cmd + r")\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", repl, tex)

    tex = braced("emph|textit", tex, r"<em>\1</em>")
    tex = braced("textbf", tex, r"<strong>\1</strong>")
    tex = braced("texttt", tex, r"<code>\1</code>")
    tex = braced("underline", tex, r"<u>\1</u>")
    tex = braced("footnote", tex, r"<sup class='fn'>[脚注: \1]</sup>")
    tex = re.sub(r"\\href\{[^}]*\}\{([^{}]*)\}", r"\1", tex)  # \href{url}{text} → text
    tex = braced("url", tex, r"<code>\1</code>")
    # 引用 → 角标片
    tex = re.sub(r"~?\\cite[pt]?\*?(?:\[[^\]]*\])?\{([^}]*)\}",
                 lambda m: "<sup class='cite'>[" + ", ".join(
                     k.strip() for k in m.group(1).split(",")) + "]</sup>", tex)
    tex = re.sub(r"\\(?:eqref|cref|Cref|ref)\{([^}]*)\}",
                 r"<span class='ref'>\1</span>", tex)
    tex = re.sub(r"\\label\{[^}]*\}", "", tex)
    # placeholder / todoresult（一参宏）
    tex = braced("placeholder", tex, r"<span class='todo'>[占位: \1]</span>")
    tex = braced("todoresult", tex, r"<span class='todo'>[TODO: \1]</span>")
    tex = re.sub(r"\\previewreleasefootnote\{\}?", "", tex)
    tex = re.sub(r"\\previewreleasefootemark\{\}?", "", tex)
    # 特殊字符
    tex = tex.replace(r"\%", "%").replace(r"\&", "&amp;").replace(r"\#", "#")
    tex = tex.replace("``", "“").replace("''", "”").replace("`", "‘").replace("'", "’")
    tex = tex.replace("~", " ").replace(r"\ ", " ")
    tex = re.sub(r"\\item\b", "• ", tex)
    # 丢弃其余未知命令但保留参数：\foo{bar} → bar（迭代两轮处理嵌套）
    for _ in range(2):
        tex = re.sub(r"\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}", r"\1", tex)
    tex = re.sub(r"\\[a-zA-Z]+\*?", "", tex)
    tex = tex.replace("{", "").replace("}", "")
    return tex


def render_text(tex: str, macros: dict[str, str]) -> str:
    """文本段 → HTML：先抽数学，再处理命令，最后按换行分段。"""
    tex = expand_text_macros(tex, macros)
    # 显示数学块
    for rx in MATH_BLOCK_RES:
        tex = rx.sub(lambda m: _math_span(m.group(0), True), tex)
    # 行内数学（先转义会破坏 $ 结构，所以先抽数学再转义文本——分片处理）
    parts = re.split(r"(<span class=\"math[^\"]*\">.*?</span>)", tex, flags=re.S)
    out = []
    for part in parts:
        if part.startswith('<span class="math'):
            out.append(part)
            continue
        seg_parts = MATH_INLINE_RE.split(part)
        for sp in seg_parts:
            if not sp:
                continue
            if MATH_INLINE_RE.fullmatch(sp):
                out.append(_math_span(sp, False))
            else:
                out.append(_simple_cmds(html.escape(sp, quote=False)))
    body = "".join(out)
    # 换行：单个换行合并为空格，空行 → <br>
    body = re.sub(r"[ \t]*\n[ \t]*", " ", body)
    body = re.sub(r"(<br>\s*)+", "<br>", body)
    return body


# ---------- 环境块 ----------

IMG_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}")
CAPTION_RE = re.compile(r"\\caption\{", re.S)


def _extract_caption(tex: str) -> str:
    m = CAPTION_RE.search(tex)
    if not m:
        return ""
    i = m.end()
    depth = 1
    start = i
    while i < len(tex) and depth:
        if tex[i] == "{":
            depth += 1
        elif tex[i] == "}":
            depth -= 1
        i += 1
    return tex[start : i - 1]


def render_block(tex: str, env: str, macros: dict[str, str]) -> str:
    """环境块 → HTML。"""
    low = env.lower()
    if low in ("figure", "wrapfigure", "figure*"):
        m = IMG_RE.search(tex)
        img = ""
        if m:
            path = m.group(1)
            img = f'<img class="fig" src="/api/figure?path={html.escape(path, quote=True)}" alt="{html.escape(path)}">'
        cap = _extract_caption(tex)
        cap_html = f'<div class="caption">{render_text(cap, macros)}</div>' if cap else ""
        return f'<div class="figure">{img}{cap_html}</div>'

    if low in ("equation", "equation*", "align", "align*", "gather", "gather*"):
        return _math_span(tex, True)

    if low in ("table", "wraptable", "table*", "longtable"):
        cap = _extract_caption(tex)
        cap_html = f'<div class="caption">{render_text(cap, macros)}</div>' if cap else ""
        body = html.escape(tex)
        return f'{cap_html}<pre class="raw">{body}</pre>'

    if low in ("algorithm", "algorithmic"):
        cap = _extract_caption(tex)
        cap_html = f'<div class="caption">{render_text(cap, macros)}</div>' if cap else ""
        return f'{cap_html}<pre class="raw">{html.escape(tex)}</pre>'

    if low == "abstract":
        inner = re.sub(r"^\\begin\{abstract\}|\\end\{abstract\}$", "", tex.strip(), flags=re.M)
        return f'<div class="abstract">{render_text(inner.strip(), macros)}</div>'

    if low == "center":
        inner = re.sub(r"^\\begin\{center\}|\\end\{center\}$", "", tex.strip(), flags=re.M)
        return f'<div class="center">{render_text(inner.strip(), macros)}</div>'

    # 其他环境：粗渲染为文本
    return render_text(tex, macros)


def render_heading(tex: str, macros: dict[str, str]) -> str:
    m = re.match(r"\\(title|section|subsection|subsubsection)(\*)?\s*", tex)
    level = {"title": 1, "section": 2, "subsection": 3, "subsubsection": 4}
    name = m.group(1) if m else "section"
    lv = level.get(name, 3)
    # 取第一个平衡花括号组
    i = tex.find("{")
    depth, start = 0, i + 1
    while i < len(tex):
        if tex[i] == "{":
            depth += 1
        elif tex[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    title = tex[start:i] if 0 <= i < len(tex) else tex
    return f"<h{lv} class='sec'>{render_text(title, macros)}</h{lv}>"


def render_segment(kind: str, env: str, tex: str, macros: dict[str, str]) -> str:
    if kind == "heading":
        return render_heading(tex, macros)
    if kind == "block":
        return render_block(tex, env, macros)
    return render_text(tex, macros)
