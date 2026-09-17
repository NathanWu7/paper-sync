r"""LaTeX 分段器：把 main.tex / text/*.tex 解析为有序段落，支持按段回写。

段落类型 (kind)：
  heading  - \section / \subsection / \subsubsection / \title（可跨行，按花括号平衡）
  block    - \begin{env}...\end{env} 原子块（figure/table/equation/algorithm/abstract 等，支持嵌套）
  text     - 空行分隔的自然段

非段落行（注释、\input、\usepackage、\newcommand 等结构行）不属于任何段落，
回写时只替换段落自身的字符区间，其余内容原样保留。
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field

# \begin 环境的块起点；document 环境不算内容
ENV_RE = re.compile(r"\\begin\{([^}]+)\}")
END_RE = re.compile(r"\\end\{([^}]+)\}")
SECTION_RE = re.compile(r"^\\(sub){0,2}section\*?\s*\{")
TITLE_RE = re.compile(r"^\\title\s*\{")

# 纯结构行（整行匹配则跳过，不构成段落）
STRUCTURAL_RE = re.compile(
    r"^\s*\\(input|include|usepackage|documentclass|newcommand|renewcommand|"
    r"providecommand|setlength|maketitle|clearpage|appendix|bibliography|"
    r"bibliographystyle|iclrfinalcopy|pagestyle|thispagestyle|author|date|"
    r"setcounter|hypersetup|graphicspath|label|raggedbottom|flushbottom|"
    r"FloatBarrier|newpage|pagebreak|vspace|vspace\*|hspace|bigskip|medskip|smallskip)\b"
)


@dataclass
class Segment:
    idx: int          # 文件内序号
    kind: str         # heading | block | text
    text: str         # 原始 LaTeX
    start: int        # 在文件字符串中的起止偏移
    end: int
    env: str = ""     # block 类型时的环境名

    @property
    def en_hash(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:12]


@dataclass
class ParsedFile:
    path: str                 # 仓库内相对路径
    content: str
    segments: list[Segment] = field(default_factory=list)


def _strip_comment(line: str) -> str:
    """去掉行内未转义的 % 注释（用于结构判断，不改变原文）。"""
    out = []
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            out.append(line[i : i + 2])
            i += 2
            continue
        if c == "%":
            break
        out.append(c)
        i += 1
    return "".join(out)


def _braces_balanced(text: str) -> bool:
    depth = 0
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return True  # 多余右括号也算结束
        i += 1
    return depth <= 0


def parse_tex(path: str, content: str) -> ParsedFile:
    """把 tex 内容解析为段落列表。main.tex 只取 document 环境内的内容。"""
    lines = content.splitlines(keepends=True)
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)

    # main.tex：只解析 \begin{document} 与 \end{document} 之间
    doc_start, doc_end = 0, len(lines)
    for i, ln in enumerate(lines):
        if "\\begin{document}" in ln:
            doc_start = i + 1
        if "\\end{document}" in ln:
            doc_end = i
            break

    segments: list[Segment] = []
    buf: list[str] = []          # 当前自然段的行
    buf_start = 0                # buf 首行的行号

    def make_seg(kind: str, start: int, raw_end: int, env: str = "") -> Segment:
        """段落文本 = content[start:raw_end] 去掉末尾换行，end 与 text 严格一致。"""
        text = content[start:raw_end].rstrip("\n")
        return Segment(len(segments), kind, text, start, start + len(text), env)

    def flush_para(upto_line: int):
        nonlocal buf, buf_start
        if not buf:
            return
        if "".join(buf).strip():
            raw_end = offsets[upto_line - 1] + len(lines[upto_line - 1])
            segments.append(make_seg("text", offsets[buf_start], raw_end))
        buf = []

    i = 0
    while i < doc_end:
        line = lines[i]
        stripped = _strip_comment(line).strip()

        # \begin{document} 之前：只拾取 \title{...}，其余（导言区）全部跳过
        if i < doc_start:
            if TITLE_RE.match(stripped):
                text = line
                j = i
                while not _braces_balanced(_strip_comment(text)) and j + 1 < doc_end:
                    j += 1
                    text += lines[j]
                segments.append(
                    make_seg("heading", offsets[i], offsets[j] + len(lines[j]))
                )
                i = j + 1
            else:
                i += 1
            continue

        # 空行 → 段落分隔
        if not stripped:
            flush_para(i)
            i += 1
            continue

        # 结构行（不在段落中时才独立跳过；段落中少见，按段落内容处理）
        if not buf and STRUCTURAL_RE.match(stripped):
            i += 1
            continue

        # 注释-only 行：独立跳过（不进段落）
        if not buf and line.lstrip().startswith("%"):
            i += 1
            continue

        # 标题行（\section / \subsection，可能跨行到花括号平衡）
        if not buf and SECTION_RE.match(stripped):
            text = line
            j = i
            while not _braces_balanced(_strip_comment(text)) and j + 1 < doc_end:
                j += 1
                text += lines[j]
            segments.append(make_seg("heading", offsets[i], offsets[j] + len(lines[j])))
            i = j + 1
            continue

        # 环境块（顶层 \begin，深度计数到配对 \end）
        m = ENV_RE.search(_strip_comment(line))
        if not buf and m and m.group(1) != "document":
            env = m.group(1)
            depth = 0
            j = i
            while j < doc_end:
                clean = _strip_comment(lines[j])
                depth += len(ENV_RE.findall(clean))
                depth -= len(END_RE.findall(clean))
                if depth <= 0:
                    break
                j += 1
            segments.append(make_seg("block", offsets[i], offsets[j] + len(lines[j]), env))
            i = j + 1
            continue

        # 普通文本行：进入段落缓冲
        if not buf:
            buf_start = i
        buf.append(line)
        i += 1

    flush_para(doc_end)

    # 重新编号（跳过的不占序号）
    for k, seg in enumerate(segments):
        seg.idx = k
    return ParsedFile(path=path, content=content, segments=segments)


def replace_segment(content: str, seg: Segment, new_text: str) -> str:
    """用新文本替换段落区间。"""
    return content[: seg.start] + new_text + content[seg.end :]


def align_segments(old_texts: list[str], new_texts: list[str]) -> dict[int, int | None]:
    """把旧段落序列对齐到新段落序列：返回 {new_idx: old_idx or None}。

    先按完全相同对齐，再对 replace 区间做相似度匹配（ratio>0.55 认为同一段被改过）。
    """
    result: dict[int, int | None] = {j: None for j in range(len(new_texts))}
    sm = difflib.SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                result[j1 + k] = i1 + k
        elif tag == "replace":
            olds = list(range(i1, i2))
            for j in range(j1, j2):
                best, best_r = None, 0.55
                for i in olds:
                    r = difflib.SequenceMatcher(None, old_texts[i], new_texts[j]).ratio()
                    if r > best_r:
                        best, best_r = i, r
                if best is not None:
                    result[j] = best
                    olds.remove(best)
    return result


if __name__ == "__main__":
    import sys
    from pathlib import Path

    repo = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    files = ["main.tex"] + sorted(
        str(p.relative_to(repo)) for p in (repo / "text").glob("*.tex")
    ) if (repo / "text").is_dir() else ["main.tex"]
    total = 0
    for rel in files:
        content = (repo / rel).read_text(encoding="utf-8")
        pf = parse_tex(rel, content)
        kinds = {}
        for s in pf.segments:
            kinds[s.kind + (f"/{s.env}" if s.kind == "block" else "")] = (
                kinds.get(s.kind + (f"/{s.env}" if s.kind == "block" else ""), 0) + 1
            )
        total += len(pf.segments)
        print(f"{rel:28s} {len(pf.segments):3d} 段  {kinds}")
        # 回写自校验：替换第 0 段为自身必须得到原文件
        if pf.segments:
            assert replace_segment(content, pf.segments[0], pf.segments[0].text) == content
    print(f"{'总计':28s} {total:3d} 段  （回写自校验通过）")
