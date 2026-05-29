"""Markdown 分块 + 占位符保护。

LLM 清洗一次只处理"一页或更小的片段"，避免：
- 长上下文导致漂移
- 一次请求 token 过多
- 表格 / 图片 / HTML 注释被 LLM 改写

策略：
1. 用 ``<!-- 第 N 页 -->`` 把整篇切成"页"
2. 每页过大时，按段落进一步切到 ``MAX_CHARS_PER_REQUEST`` 以下
3. 在送给 LLM 前，把表格 / 图片 / HTML 注释 / 公式替换为占位符
4. LLM 返回后再还原
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import MAX_CHARS_PER_REQUEST

PAGE_MARKER_PAT = re.compile(r"<!--\s*第\s*(\d+)\s*页\s*-->")

# 与 rules._PROTECTED_REGEX 同步：需要原样保留的片段
_PROTECTED_REGEX = re.compile(
    r"(<table\b[^>]*>[\s\S]*?</table>)"
    r"|(!\[[^\]]*\]\([^)]*\)(?:\s*<!--[^>]*-->)?)"
    r"|(<!--[\s\S]*?-->)"
    r"|(\$\$[\s\S]*?\$\$)"
    r"|(\$[^\n$]+?\$)",
    re.IGNORECASE,
)


@dataclass
class Placeholder:
    token: str
    payload: str


@dataclass
class Chunk:
    """一个待清洗的片段。"""
    page_no: int | None       # 所属页码；None 表示"页前 / 页后"
    seq: int                  # 同页内的顺序（拆分大页时使用）
    text: str                 # 已替换为占位符、可以送给 LLM 的内容
    placeholders: list[Placeholder] = field(default_factory=list)


# ----------------- 占位符 -----------------
def mask_protected(text: str) -> tuple[str, list[Placeholder]]:
    """把表格/图片/HTML注释/公式替换成占位符。"""
    holders: list[Placeholder] = []

    def _label(kind: str, idx: int) -> str:
        return f"<<{kind}_{idx}>>"

    def _kind_for(match_text: str) -> str:
        s = match_text.lstrip().lower()
        if s.startswith("<table"):
            return "TAB"
        if s.startswith("!["):
            return "IMG"
        if s.startswith("<!--"):
            return "CMT"
        if s.startswith("$$"):
            return "MATH"
        return "MATH"

    def _sub(m: re.Match) -> str:
        kind = _kind_for(m.group(0))
        token = _label(kind, len(holders))
        holders.append(Placeholder(token=token, payload=m.group(0)))
        return token

    masked = _PROTECTED_REGEX.sub(_sub, text)
    return masked, holders


def unmask_protected(text: str, holders: list[Placeholder]) -> str:
    """把占位符再换回原内容；找不到的占位符也容错保留。"""
    for h in holders:
        text = text.replace(h.token, h.payload)
    return text


# ----------------- 按页切 -----------------
def split_by_page(md: str) -> list[tuple[int | None, str]]:
    """按 ``<!-- 第 N 页 -->`` 切分。

    返回 [(page_no, page_content_含末尾标记)]；标记之前没有内容时 page_no=None。
    """
    parts: list[tuple[int | None, str]] = []
    last_pos = 0
    last_page: int | None = None
    for m in PAGE_MARKER_PAT.finditer(md):
        chunk = md[last_pos: m.end()]
        parts.append((last_page, chunk))
        last_page = int(m.group(1))
        last_pos = m.end()
    tail = md[last_pos:]
    if tail.strip():
        parts.append((last_page, tail))
    return parts


# ----------------- 大页继续切 -----------------
def _split_by_size(text: str, max_chars: int) -> list[str]:
    """按段落把单页继续切到 max_chars 以下。

    优先以连续空行（段落边界）切；若仍过长，按 max_chars 硬切。
    """
    if len(text) <= max_chars:
        return [text]

    paragraphs = re.split(r"(\n{2,})", text)
    out: list[str] = []
    buf = ""
    for piece in paragraphs:
        if len(buf) + len(piece) <= max_chars:
            buf += piece
        else:
            if buf:
                out.append(buf)
            if len(piece) <= max_chars:
                buf = piece
            else:
                # 硬切
                for i in range(0, len(piece), max_chars):
                    out.append(piece[i: i + max_chars])
                buf = ""
    if buf:
        out.append(buf)
    return out


# ----------------- 总入口 -----------------
def chunk_markdown(md: str, max_chars: int = MAX_CHARS_PER_REQUEST) -> list[Chunk]:
    """把整篇 markdown 切为 LLM 可处理的若干 Chunk。"""
    chunks: list[Chunk] = []
    for page_no, page_text in split_by_page(md):
        sub_parts = _split_by_size(page_text, max_chars)
        for seq, sub in enumerate(sub_parts):
            masked, holders = mask_protected(sub)
            chunks.append(
                Chunk(page_no=page_no, seq=seq, text=masked, placeholders=holders)
            )
    return chunks


def reassemble(chunks_with_text: list[tuple[Chunk, str]]) -> str:
    """把每个 Chunk 的处理结果还原 + 顺序拼接。"""
    out: list[str] = []
    for chunk, processed in chunks_with_text:
        out.append(unmask_protected(processed, chunk.placeholders))
    text = "".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
