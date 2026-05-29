"""DOCX / DOC 文档提取器。

- .docx：直接用 python-docx 解析段落 + 表格，按 Heading 样式映射 markdown 标题
- .doc ：Windows 下通过 Word COM 转 .docx 再走 docx 流程
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.document import Document as _Doc
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph
from loguru import logger

from utils.markdown_utils import (
    cells_to_html_table,
    clean_line,
    guess_heading_level,
    to_heading,
)


# --------- doc -> docx 转换 ---------
def _convert_doc_to_docx(doc_path: Path) -> Path:
    """在 Windows 上用 Word COM 把 .doc 转换为 .docx，返回新文件路径（临时目录）。"""
    try:
        import pythoncom  # noqa: F401  pywin32
        import win32com.client  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("缺少 pywin32，无法在 Windows 转换 .doc 文件") from e

    tmp_dir = Path(tempfile.mkdtemp(prefix="doc2docx_"))
    out_path = tmp_dir / (doc_path.stem + ".docx")

    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(str(doc_path.resolve()), ReadOnly=True)
        # wdFormatXMLDocument = 12 (.docx)
        doc.SaveAs(str(out_path.resolve()), FileFormat=12)
        doc.Close(False)
    finally:
        word.Quit()
    return out_path


# --------- 顺序遍历 docx 中段落与表格 ---------
def _iter_block_items(parent: _Doc):
    """按文档顺序产出段落与表格。"""
    if hasattr(parent, "element"):
        body = parent.element.body
    elif hasattr(parent, "_tc"):
        body = parent._tc
    else:
        raise ValueError("unknown parent")
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


# --------- 段落 -> markdown ---------
_STYLE_HEADING_PAT = re.compile(r"^Heading\s*(\d+)", re.IGNORECASE)


def _paragraph_to_md(p: Paragraph) -> str:
    text = clean_line(p.text)
    if not text:
        return ""

    style_name = (p.style.name if p.style is not None else "") or ""
    m = _STYLE_HEADING_PAT.match(style_name)
    if m:
        level = min(int(m.group(1)), 6)
        return to_heading(text, level)
    if style_name.lower() in {"title"}:
        return to_heading(text, 1)

    level = guess_heading_level(text)
    if level:
        return to_heading(text, level)

    parts: list[str] = []
    has_any_run = False
    for run in p.runs:
        if not run.text:
            continue
        has_any_run = True
        t = clean_line(run.text)
        if not t:
            continue
        if run.bold and not text.startswith("#"):
            t = f"**{t}**"
        parts.append(t)
    if has_any_run and parts:
        return " ".join(parts)
    return text


# --------- 表格 -> HTML ---------
def _table_to_html(tbl: Table) -> str:
    """处理 docx 表格，含 rowspan / colspan 合并。"""
    grid = tbl._tbl  # CT_Tbl
    rows = tbl.rows
    n_rows = len(rows)
    n_cols = max((len(r.cells) for r in rows), default=0)
    if n_rows == 0 or n_cols == 0:
        return ""

    cell_text: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    cell_span: list[list[tuple[int, int]]] = [[(1, 1) for _ in range(n_cols)] for _ in range(n_rows)]
    skip: set[tuple[int, int]] = set()
    seen_tc: dict[int, tuple[int, int]] = {}

    for r_idx, row in enumerate(rows):
        for c_idx, cell in enumerate(row.cells):
            if (r_idx, c_idx) in skip:
                continue
            tc_id = id(cell._tc)
            if tc_id in seen_tc:
                # 合并单元格在 python-docx 中会重复出现，跳过
                skip.add((r_idx, c_idx))
                continue
            seen_tc[tc_id] = (r_idx, c_idx)

            text = "\n".join(
                clean_line(p.text) for p in cell.paragraphs if clean_line(p.text)
            )
            cell_text[r_idx][c_idx] = text

            # 估算 colspan：同一行连续重复的 tc
            colspan = 1
            for cc in range(c_idx + 1, n_cols):
                try:
                    nxt = row.cells[cc]
                except IndexError:
                    break
                if id(nxt._tc) == tc_id:
                    colspan += 1
                    skip.add((r_idx, cc))
                else:
                    break

            # 估算 rowspan：后续行同列位置重复的 tc
            rowspan = 1
            for rr in range(r_idx + 1, n_rows):
                try:
                    other = rows[rr].cells[c_idx]
                except IndexError:
                    break
                if id(other._tc) == tc_id:
                    rowspan += 1
                    for cc in range(c_idx, c_idx + colspan):
                        skip.add((rr, cc))
                else:
                    break

            cell_span[r_idx][c_idx] = (rowspan, colspan)

    # 删除被合并位置（按 skip 重组每行）
    out_rows: list[list[str]] = []
    out_spans: list[list[tuple[int, int]]] = []
    for r_idx in range(n_rows):
        row_t: list[str] = []
        row_s: list[tuple[int, int]] = []
        for c_idx in range(n_cols):
            if (r_idx, c_idx) in skip:
                continue
            row_t.append(cell_text[r_idx][c_idx])
            row_s.append(cell_span[r_idx][c_idx])
        if row_t:
            out_rows.append(row_t)
            out_spans.append(row_s)

    return cells_to_html_table(out_rows, out_spans)


# --------- 主入口 ---------
@dataclass
class DocxStats:
    file: str = ""
    converted_from_doc: bool = False
    n_paragraphs: int = 0
    n_headings: int = 0
    n_tables: int = 0
    n_chars: int = 0
    elapsed_sec: float = 0.0


def extract_docx(file_path: Path) -> tuple[str, DocxStats]:
    """从 .docx / .doc 文件提取并返回 (markdown 字符串, 统计信息)。"""
    file_path = Path(file_path)
    ext = file_path.suffix.lower()
    cleanup_dir: Path | None = None
    stats = DocxStats(file=file_path.name)

    t0 = time.time()
    if ext == ".doc":
        logger.info(f"[DOCX] 转换 .doc -> .docx：{file_path.name}")
        docx_path = _convert_doc_to_docx(file_path)
        cleanup_dir = docx_path.parent
        stats.converted_from_doc = True
    elif ext == ".docx":
        docx_path = file_path
    else:
        raise ValueError(f"不支持的扩展名：{ext}")

    logger.info(f"[DOCX] 开始解析 {file_path.name}")

    try:
        doc = Document(str(docx_path))
        title = file_path.stem
        out: list[str] = [f"# {title}", ""]

        for block in _iter_block_items(doc):
            if isinstance(block, Paragraph):
                md = _paragraph_to_md(block)
                if md:
                    out.append(md)
                    out.append("")
                    stats.n_paragraphs += 1
                    if md.lstrip().startswith("#"):
                        stats.n_headings += 1
            elif isinstance(block, Table):
                html = _table_to_html(block)
                if html:
                    out.append("")
                    out.append(html)
                    out.append("")
                    stats.n_tables += 1

        text = "\n".join(out)
        text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
        stats.n_chars = len(text)
        stats.elapsed_sec = time.time() - t0
        logger.success(
            f"[DOCX] 完成 {file_path.name} | "
            f"段落={stats.n_paragraphs} 标题={stats.n_headings} 表格={stats.n_tables} "
            f"字符={stats.n_chars} 耗时={stats.elapsed_sec:.1f}s"
            + (" | 由 .doc 转换" if stats.converted_from_doc else "")
        )
        return text, stats
    finally:
        if cleanup_dir is not None and cleanup_dir.exists():
            shutil.rmtree(cleanup_dir, ignore_errors=True)
