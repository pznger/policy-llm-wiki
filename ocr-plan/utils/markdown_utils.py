"""Markdown 拼装相关的辅助函数。

参考 raw 目录下《混凝土结构设计规范.md》的格式：
- 多级 # 标题
- 页码标注用 HTML 注释 <!-- 第 N 页 -->
- 图片使用相对路径，并在后面跟 <!-- desc: ... --> 描述
"""
from __future__ import annotations

import re
from typing import Iterable

from config import H1_MAX_LEN, H2_MAX_LEN, H3_MAX_LEN

# -------- 启发式标题识别 --------
_H1_PAT = re.compile(r"^第[一二三四五六七八九十百零\d]+[章篇编]\b|^第[一二三四五六七八九十百零\d]+\s*章")
_H2_PAT = re.compile(r"^第[一二三四五六七八九十百零\d]+[节]\b|^\d+\.\d+\s")
_H3_PAT = re.compile(r"^\d+\.\d+\.\d+\s")
_ARTICLE_PAT = re.compile(r"^第[一二三四五六七八九十百零\d]+条\b")


def guess_heading_level(line: str) -> int | None:
    """对一行文本判断它可能是第几级标题；不是标题返回 None。"""
    s = line.strip()
    if not s or len(s) > H3_MAX_LEN:
        return None
    if _H1_PAT.match(s) and len(s) <= H1_MAX_LEN:
        return 1
    if _H2_PAT.match(s) and len(s) <= H2_MAX_LEN:
        return 2
    if _H3_PAT.match(s) and len(s) <= H3_MAX_LEN:
        return 3
    if _ARTICLE_PAT.match(s) and len(s) <= H3_MAX_LEN:
        return 4
    return None


def to_heading(line: str, level: int) -> str:
    return f"{'#' * level} {line.strip()}"


# -------- 表格 --------
def cells_to_html_table(
    rows: Iterable[Iterable[str]],
    spans: Iterable[Iterable[tuple[int, int]]] | None = None,
) -> str:
    """把二维单元格转成 HTML 表格字符串。

    rows: 每行单元格文本（None / "" 会显示为空）
    spans: 与 rows 对齐，每个单元 (rowspan, colspan)；None 表示无合并
    """
    rows = [list(r) for r in rows]
    if spans is None:
        spans = [[(1, 1)] * len(r) for r in rows]
    else:
        spans = [list(s) for s in spans]

    out: list[str] = ['<table border="1" >']
    for row, sp in zip(rows, spans):
        out.append("<tr>")
        for cell, (rs, cs) in zip(row, sp):
            text = (cell or "").replace("\n", "<br>").strip()
            attrs = ""
            if rs and rs > 1:
                attrs += f' rowspan="{rs}"'
            if cs and cs > 1:
                attrs += f' colspan="{cs}"'
            out.append(f"<td{attrs}>{text}</td>")
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def pdfplumber_table_to_html(table: list[list[str | None]]) -> str:
    """pdfplumber 抽出来的 table 是 list[list[str|None]]，没有合并信息。"""
    return cells_to_html_table(table)


# -------- 页码 --------
def page_marker(page_no: int) -> str:
    return f"<!-- 第 {page_no} 页 -->"


# -------- 文本清洗 --------
def clean_line(line: str) -> str:
    """规范化空白与控制字符。"""
    s = line.replace("\u3000", " ").replace("\xa0", " ")
    s = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]", "", s)
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def paragraphs_to_md(lines: list[str]) -> str:
    """把若干文本行装配为 markdown 片段，自动识别标题。"""
    out: list[str] = []
    for raw in lines:
        line = clean_line(raw)
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        level = guess_heading_level(line)
        if level:
            out.append("")
            out.append(to_heading(line, level))
            out.append("")
        else:
            out.append(line)
            out.append("")
    md = "\n".join(out).strip()
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md
