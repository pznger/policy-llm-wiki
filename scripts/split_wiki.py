#!/usr/bin/env python3
"""把一份 raw/<规范名>.md 按"章 / 节"拆分到 wiki/<规范名>/。

适用于医疗器械、药品监管等政策法规类文档。和建筑规范的区别在于：

- 一级标题是"第X章 / 附则 / 附录X / 附件X"，而不是 ``# 1 总则``
- 二级标题是"第X节 …"或"X.X …"
- "第X条 …"、"X.X.X …" 一律视为正文，不参与拆分
- 有 ``##`` 二级标题时以节为文件单元（同 ccic-llm-wiki）；节超过 2 页再拆为 ``-1``、``-2`` …

详见 ``wiki-拆分指导.md``。
"""
from __future__ import annotations

import argparse
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


# ------------------ 正则 ------------------
# 整行仅页码（用于判断是否跳过空页等）
PAGE_ONLY_RE = re.compile(
    r"^<!--\s*(?:第\s*)?[·•.]?\s*(\d+)\s*[·•.]?\s*(?:页)?\s*-->\s*$"
)
# 行内页码（OCR 常把正文接在页码注释后）
PAGE_MARKER_RE = re.compile(
    r"<!--\s*(?:第\s*)?[·•.]?\s*(\d+)\s*[·•.]?\s*(?:页)?\s*-->"
)

HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$")

_CN_NUM = "一二三四五六七八九十百零〇两"
CHAPTER_RE = re.compile(
    rf"^第\s*([{_CN_NUM}\d]+)\s*[章编]\s*(.*)$"
)
APPENDIX_RE = re.compile(r"^附\s*则\s*(.*)$")
APPENDIX_X_RE = re.compile(r"^附\s*([录件])\s*([A-Za-z\d{_CN_NUM}]*)?\s*(.*)$")

SECTION_CN_RE = re.compile(
    rf"^第\s*([{_CN_NUM}\d]+)\s*节\s*(.*)$"
)
SECTION_NO_RE = re.compile(r"^(\d+\.\d+)(?=\s|[^\d.]|$)\s*(.*)$")

ARTICLE_RE = re.compile(rf"^第\s*([{_CN_NUM}\d]+)\s*条")
SUBSECTION_NO_RE = re.compile(r"^(\d+\.\d+\.\d+)(?=\s|[^\d.]|$)")

# 无「第X章」时，``# 2.范围``、``# 3.适用法规`` 等数字一级标题视为「篇」
NUMBERED_PART_H1_RE = re.compile(r"^(\d+)(?:[.\s、．]|$)")

TABLE_OPEN_RE = re.compile(r"<table\b", re.I)
TABLE_CLOSE_RE = re.compile(r"</table>", re.I)

TAIL_HEADING_RE = re.compile(
    r"^(本规范用词说明|引用标准名录|条文说明|修订说明|本标准用词说明)$"
)

_CN_NUM = "一二三四五六七八九十百千零〇两"
CN_CHAPTER_RE = re.compile(rf"^([{_CN_NUM}]+)、\s*(.*)$")
CN_SECTION_RE = re.compile(rf"^（([{_CN_NUM}]+)）")
CN_SECTION_ASCII_RE = re.compile(rf"^\(([{_CN_NUM}]+)\)")
CN_APPENDIX_RE = re.compile(r"^附[：:]\s*(.*)$")
TABLE_CAPTION_RE = re.compile(
    r"^表格\s*(?:[（(]\s*第\s*\d+\s*页\s*[）)])?\s*$"
)


# ------------------ 数据结构 ------------------
@dataclass
class Section:
    title: str
    start_idx: int
    start_page: int
    end_idx: int = 0


@dataclass
class SectionGroup:
    """一个正文文件对应的一个或多个二级节（过小则合并）。"""

    sections: list[Section]
    merged: bool
    file_stem: str


@dataclass
class Chapter:
    title: str
    start_idx: int
    start_page: int
    sections: list[Section] = field(default_factory=list)
    end_idx: int = 0


@dataclass
class BodyFileEntry:
    """根索引「各文件摘要」中的一项（对应一个正文文件）。"""

    title: str
    rel_path: str
    start_page: int = 0
    end_page: int = 0


@dataclass
class OutlineEntry:
    """条目录 / 条文摘要 中的一条（仅条号 + 页码，不写条文原文截断）。"""

    label: str
    page: int


@dataclass
class TableSpan:
    """HTML 表格在源文中的行范围（不可从中间切开）。"""

    start_idx: int
    end_idx: int
    label: str
    start_page: int
    end_page: int


# LLM 补写占位说明（写入 -index.md，补全时替换整段 TODO 后文字）
SUMMARY_TODO = (
    "TODO: 待LLM补写——本条/本节主题与核心义务（约 50 字、1～2 句，供检索定位；"
    "具体数值与表格见正文）"
)
NAME_TODO = (
    "TODO: 待LLM命名——根据 -index 中「合并来源」概括为 8～24 字的文件名"
    "（重命名 .md 与 -index.md，并同步根索引文件目录；正文勿改）"
)

# 二级节过小则合并：低于阈值则与相邻小节并入同一文件
MIN_SECTION_BODY_LINES = 8
MIN_SECTION_BODY_CHARS = 350
MAX_MERGED_SECTIONS = 15
MERGED_NAME_TODO_PREFIX = "TODO-待LLM命名"


# ------------------ 标题归一化 ------------------
def _strip_cn_token_spaces(text: str) -> str:
    def _norm(m: re.Match) -> str:
        body = re.sub(r"\s+", "", m.group(1))
        return "第" + body + m.group(2)

    text = re.sub(
        rf"第\s*([{_CN_NUM}\d]+(?:\s+[{_CN_NUM}\d]+)*)\s*([章节条编项])",
        _norm,
        text,
    )
    return text


def normalize_inline_ocr(text: str) -> str:
    """最小 OCR 纠错（借鉴 ccic-llm-wiki）。"""
    text = re.sub(r"(\d+)\s*[．.]\s+\$([0-9][^$]*)\$", r"$\1.\2$", text)
    text = re.sub(r"(\$\s*[^$]+?\$)\s*、\s*的", r"\1 的", text)
    text = text.replace(
        "$25\\mathrm {\\;Pa}\\;30\\mathrm {\\;Pa}$",
        "$25\\mathrm {\\;Pa}~30\\mathrm {\\;Pa}$",
    )
    return text


def normalize_title(text: str) -> str:
    t = normalize_inline_ocr(text.strip().replace("\u3000", " "))
    t = t.replace("．", ".")
    t = _strip_cn_token_spaces(t)
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    t = re.sub(r"^(\d+\.\d+\.\d+)(?=[^\d.\s])", r"\1 ", t)
    t = re.sub(r"^(\d+\.\d+)(?=[^\d.\s])", r"\1 ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def slug_title(text: str) -> str:
    t = normalize_title(text)
    t = t.replace(" ", "").replace("/", "／").replace(":", "：")
    t = t.replace("\\", "＼").replace("*", "＊").replace("?", "？")
    t = t.replace('"', "”").replace("<", "＜").replace(">", "＞")
    t = t.replace("|", "｜")
    return t


# 文件名中编号后的标题最长字符数（借鉴 ccic：``2.1性能要求``，避免整句作文件名）
SECTION_LABEL_MAX = 28


def short_slug_title(text: str, *, label_max: int = SECTION_LABEL_MAX) -> str:
    """``2.1`` + 短标题；过长条文式标题截断。"""
    base = slug_title(text)
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", base)
    if m:
        num, rest = m.group(1), m.group(2)
        if len(rest) > label_max:
            rest = rest[:label_max]
        return num + rest
    if len(base) > label_max + 8:
        return base[: label_max + 8]
    return base


def chapter_dir_slug(chapter_title: str) -> str:
    return short_slug_title(chapter_title, label_max=32)


def chapter_body_stem(chapter_title: str) -> str:
    """章内无 ``##`` 时正文文件名（ccic：``1总则/总则.md``）。"""
    t = normalize_title(chapter_title)
    cm = CHAPTER_RE.match(t)
    if cm:
        tail = (cm.group(2) or "").strip()
        if tail:
            return short_slug_title(tail, label_max=32)
    return short_slug_title(t, label_max=32)


def section_file_stem(section_title: str) -> str:
    return short_slug_title(section_title, label_max=SECTION_LABEL_MAX)


def catalog_title(text: str) -> str:
    return slug_title(text)


def is_tail_heading(title: str) -> bool:
    return bool(TAIL_HEADING_RE.match(title.replace(" ", "")))


def page_no_from_line(line: str) -> int | None:
    m = PAGE_MARKER_RE.search(line.strip())
    return int(m.group(1)) if m else None


def page_after_marker(marker_page: int) -> int:
    """OCR 在每页**末尾**插入 ``<!-- 第 N 页 -->``（见 ocr-plan）；标记之后的内容属于第 N+1 页。"""
    return marker_page + 1


def advance_page_on_line(line: str, current_page: int) -> int:
    """根据行内页码标记更新「当前正文所在页」。"""
    page_no = page_no_from_line(line)
    if page_no is None:
        return current_page
    return max(current_page, page_after_marker(page_no))


def line_body_after_page_marker(line: str) -> str:
    """去掉行首页码注释后的正文，用于识别 ``<!-- 第 N 页 -->第五条…`` 同行条款。"""
    stripped = line.strip().replace("**", "")
    m = PAGE_MARKER_RE.search(stripped)
    if m:
        return stripped[m.end() :].strip()
    return stripped


# ------------------ 判断 ------------------
def is_chapter_title(title: str) -> bool:
    t = title.strip()
    if CHAPTER_RE.match(t):
        return True
    if APPENDIX_RE.match(t):
        return True
    if APPENDIX_X_RE.match(t):
        return True
    return False


def is_section_title(title: str, *, level: int | None = None) -> bool:
    """二级拆分单元：``第X节``、``X.X``，或 Markdown ``##`` 下的节标题。"""
    t = title.strip()
    if SECTION_CN_RE.match(t):
        return True
    if SUBSECTION_NO_RE.match(t):
        return False
    if SECTION_NO_RE.match(t):
        return True
    if level == 2:
        if is_chapter_title(t) or is_numbered_part_h1(t):
            return False
        clause_markers = ("应当", "不得", "报告单位", "以下规定")
        if any(m in t for m in clause_markers) and len(t) > 36:
            return False
    return False


def is_markdown_section(level: int, title: str) -> bool:
    if level == 2 and is_chinese_chapter_title(title):
        return False
    return level == 2 and is_section_title(title, level=level)


def strip_markdown_emphasis(text: str) -> str:
    return re.sub(r"\*+", "", text).strip()


def line_plain_text(raw: str) -> str:
    body = line_body_after_page_marker(raw)
    return normalize_title(strip_markdown_emphasis(body))


def is_chinese_chapter_title(title: str) -> bool:
    return bool(CN_CHAPTER_RE.match(title.strip()))


def is_chinese_section_title(title: str) -> bool:
    t = title.strip()
    return bool(CN_SECTION_RE.match(t) or CN_SECTION_ASCII_RE.match(t))


def chinese_chapter_title_from_plain(plain: str) -> str | None:
    m = CN_CHAPTER_RE.match(plain)
    if not m:
        return None
    num, rest = m.group(1), (m.group(2) or "").strip()
    if not rest:
        return f"{num}、"
    for sep in ("。", "；", "\n"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    return f"{num}、{rest[:18]}"


def chinese_section_title_from_plain(plain: str) -> str | None:
    m = CN_SECTION_RE.match(plain) or CN_SECTION_ASCII_RE.match(plain)
    if not m:
        return None
    prefix = m.group(0)
    rest = plain[m.end() :].strip()
    if not rest:
        return prefix
    for sep in ("。", "；", "本指导", "本原则", "常见", "良好"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    return (prefix + rest[:18])[:28]


def should_split_chinese_paren_section(
    chapter: Chapter | None, plain: str
) -> bool:
    """``第X章`` 下的 ``（一）`` 为法条列举项，不拆成独立二级文件。"""
    if chapter is None or not chinese_section_title_from_plain(plain):
        return False
    if is_chapter_title(chapter.title):
        return False
    return True


def is_spec_document_title(title: str, level: int, src_name: str = "") -> bool:
    """规范总标题 ``# 某某指导原则（试行）``，不作为章。"""
    if level != 1:
        return False
    if is_chapter_title(title) or is_numbered_part_h1(title) or is_chinese_chapter_title(
        title
    ):
        return False
    if src_name:
        sn, tn = slug_title(src_name), slug_title(title)
        if sn and len(sn) > 8 and sn in tn:
            return True
    return len(title) > 32 and not re.match(r"^\d", title)


def is_article_or_subsection(title: str) -> bool:
    t = title.strip()
    return bool(ARTICLE_RE.match(t)) or bool(SUBSECTION_NO_RE.match(t))


def is_numbered_part_h1(title: str) -> bool:
    """``# 2.范围``、``# 3.适用法规`` 等篇级标题（非 ``3.5经省级…`` 条款行）。"""
    if is_chapter_title(title):
        return False
    t = title.strip()
    if len(t) > 80:
        return False
    if re.match(r"^\d+\.\d+\.\d+", t):
        return False
    clause_markers = ("应当", "不得", "报告单位", "以下规定", "包括", "有下列")
    if any(m in t for m in clause_markers):
        return False
    # ``3.5经省级…``：二级编号 + 长正文 → 条款
    if re.match(r"^\d+\.\d+[\u4e00-\u9fff]", t) and len(t) > 22:
        return False
    if re.match(r"^\d+[.\s、．]?\s*[\u4e00-\u9fff（(][^\n]{1,40}$", t):
        return True
    if re.match(r"^\d+\s+[\u4e00-\u9fff]{2,25}$", t):
        return True
    return False


def is_document_part_title(title: str, level: int) -> bool:
    """拆分单元：第X章 / 数字篇 ``2.范围`` / 中文 ``一、概述``（可为 ``#`` 或 ``##``）。"""
    if is_chinese_chapter_title(title):
        return level in (1, 2)
    return level == 1 and (is_chapter_title(title) or is_numbered_part_h1(title))


# ------------------ 解析结构 ------------------
def first_content_index(lines: list[str], src_name: str = "") -> int | None:
    """正文起点：第一个章级标题（含 ``## 一、`` 或无 # 的 ``二、…``）。"""
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            title = normalize_title(m.group(2))
            level = len(m.group(1))
            if is_spec_document_title(title, level, src_name):
                continue
            if is_document_part_title(title, level):
                return idx
            continue
        plain = line_plain_text(line)
        if plain and chinese_chapter_title_from_plain(plain):
            return idx
    return None


def _start_chapter(
    chapters: list[Chapter],
    *,
    title: str,
    start_idx: int,
    start_page: int,
) -> Chapter:
    chap = Chapter(title=title, start_idx=start_idx, start_page=start_page)
    chapters.append(chap)
    return chap


def parse_structure(lines: list[str], src_name: str = "") -> tuple[list[Chapter], int]:
    start_idx = first_content_index(lines, src_name)
    if start_idx is None:
        return [], 0

    chapters: list[Chapter] = []
    current_page = 1
    current_chapter: Chapter | None = None
    content_end_idx = len(lines)

    for idx in range(start_idx, len(lines)):
        raw = lines[idx]
        current_page = advance_page_on_line(raw, current_page)

        heading_match = HEADING_RE.match(raw)
        if heading_match:
            level = len(heading_match.group(1))
            title = normalize_title(heading_match.group(2))

            if is_tail_heading(title):
                content_end_idx = idx
                break

            if is_article_or_subsection(title):
                continue

            if is_document_part_title(title, level):
                current_chapter = _start_chapter(
                    chapters,
                    title=title,
                    start_idx=idx,
                    start_page=current_page,
                )
                continue

            if current_chapter is not None and is_markdown_section(level, title):
                current_chapter.sections.append(
                    Section(title=title, start_idx=idx, start_page=current_page)
                )
                continue

            if current_chapter is not None and is_chinese_section_title(title):
                if not is_chapter_title(current_chapter.title):
                    current_chapter.sections.append(
                        Section(title=title, start_idx=idx, start_page=current_page)
                    )
                continue
            continue

        plain = line_plain_text(raw)
        if not plain:
            continue

        if is_tail_heading(plain.replace(" ", "")):
            content_end_idx = idx
            break

        chap_title = chinese_chapter_title_from_plain(plain)
        if chap_title:
            current_chapter = _start_chapter(
                chapters,
                title=chap_title,
                start_idx=idx,
                start_page=current_page,
            )
            continue

        if CN_APPENDIX_RE.match(plain):
            short = plain[:48] if len(plain) > 48 else plain
            current_chapter = _start_chapter(
                chapters,
                title=short,
                start_idx=idx,
                start_page=current_page,
            )
            continue

        if current_chapter is not None and should_split_chinese_paren_section(
            current_chapter, plain
        ):
            sec_title = chinese_section_title_from_plain(plain)
            assert sec_title
            current_chapter.sections.append(
                Section(title=sec_title, start_idx=idx, start_page=current_page)
            )
            continue

        _ = plain

    for ci, chap in enumerate(chapters):
        chap.end_idx = (
            chapters[ci + 1].start_idx
            if ci + 1 < len(chapters)
            else content_end_idx
        )
        for si, sec in enumerate(chap.sections):
            sec.end_idx = (
                chap.sections[si + 1].start_idx
                if si + 1 < len(chap.sections)
                else chap.end_idx
            )

    return chapters, start_idx


# ------------------ 页数与切分 ------------------
def pages_in_segment(segment_lines: list[str], start_page: int) -> list[int]:
    """统计段内实际承载正文的页码列表。

    政策类 OCR 常把正文与 ``<!-- 第 N 页 -->`` 写在同一行；遇到页码标记时也要
    把该页计入，不能等下一行正文才记页。
    """
    pages: list[int] = []
    current_page = start_page

    def _touch_page(page_no: int) -> None:
        nonlocal current_page
        if page_no >= current_page:
            current_page = page_no
        if not pages or pages[-1] != current_page:
            pages.append(current_page)

    for line in segment_lines:
        stripped = line.strip()
        page_no = page_no_from_line(stripped)
        if page_no is not None:
            _touch_page(page_no)
            current_page = page_no + 1
            body = line_body_after_page_marker(line)
            if body.strip():
                _touch_page(current_page)
            continue
        if not stripped:
            continue
        _touch_page(current_page)
    return pages


def split_segment_chunks(
    *,
    start_idx: int,
    end_idx: int,
    start_page: int,
    content: list[str],
    max_pages: int,
) -> list[dict]:
    """按页把一段（章或节）切成多个 chunk。"""
    segment = content[start_idx:end_idx]
    pages = pages_in_segment(segment, start_page)
    if not pages:
        return [{
            "suffix": "",
            "start_idx": start_idx,
            "end_idx": end_idx,
            "start_page": start_page,
            "end_page": start_page,
            "part_no": 1,
            "parts": 1,
            "is_table": False,
        }]

    page_count = len(pages)
    if page_count <= max_pages:
        return [{
            "suffix": "",
            "start_idx": start_idx,
            "end_idx": end_idx,
            "start_page": start_page,
            "end_page": pages[-1],
            "part_no": 1,
            "parts": 1,
            "is_table": False,
        }]

    ranges: list[tuple[int, int]] = []
    range_start = start_page
    while range_start <= pages[-1]:
        range_end = min(range_start + max_pages - 1, pages[-1])
        ranges.append((range_start, range_end))
        range_start = range_end + 1

    chunks: list[dict] = []
    for part_no, (page_start, page_end) in enumerate(ranges, 1):
        actual_start_page = (
            page_start if part_no == 1
            else max(page_start - 1, start_page)
        )
        first_line = start_idx
        last_line = end_idx
        seen = False
        current_page = start_page
        for idx in range(start_idx, end_idx):
            page_no = page_no_from_line(content[idx])
            if page_no is not None:
                current_page = max(current_page, page_after_marker(page_no))
            if not seen and current_page >= actual_start_page:
                first_line = idx
                seen = True
            elif seen and current_page > page_end:
                last_line = idx
                break
        if part_no == 1:
            first_line = start_idx
        chunks.append({
            "suffix": f"-{part_no}",
            "start_idx": first_line,
            "end_idx": last_line,
            "start_page": actual_start_page,
            "end_page": page_end,
            "part_no": part_no,
            "parts": len(ranges),
            "is_table": False,
        })
    return chunks


def _line_is_table_caption_only(text: str) -> bool:
    t = strip_markdown_emphasis(line_body_after_page_marker(text))
    if not t:
        return True
    return bool(TABLE_CAPTION_RE.match(t))


def _segment_is_table_filler(content: list[str], start_idx: int, end_idx: int) -> bool:
    """表格之间的页码行、``**表格（第N页）**`` 等，不单独成文件。"""
    has_content = False
    for idx in range(start_idx, end_idx):
        raw = content[idx]
        body = strip_markdown_emphasis(line_body_after_page_marker(raw))
        if not body:
            continue
        if _line_is_table_caption_only(raw):
            continue
        if len(body) < 4:
            continue
        has_content = True
        break
    return not has_content


def _infer_table_label(lines: list[str], table_start: int) -> str:
    for i in range(table_start - 1, max(-1, table_start - 8), -1):
        raw = lines[i].strip()
        if not raw:
            continue
        page_m = re.search(
            r"表格\s*[（(]\s*第\s*(\d+)\s*页\s*[）)]", strip_markdown_emphasis(raw)
        )
        if page_m:
            return f"表格第{page_m.group(1)}页"
        body = strip_markdown_emphasis(line_body_after_page_marker(raw))
        if not body or _line_is_table_caption_only(raw):
            continue
        hm = HEADING_RE.match(lines[i])
        if hm:
            t = normalize_title(hm.group(2))
            return catalog_title(t)[:36]
        if "表" in body and len(body) < 80 and not TABLE_CAPTION_RE.match(body):
            return catalog_title(body)[:36]
    return ""


def find_table_spans(lines: list[str], *, start_page: int = 1) -> list[TableSpan]:
    spans: list[TableSpan] = []
    current_page = start_page
    table_no = 0
    i = 0
    while i < len(lines):
        current_page = advance_page_on_line(lines[i], current_page)
        if TABLE_OPEN_RE.search(lines[i]):
            start = i
            table_no += 1
            label = _infer_table_label(lines, start) or f"表{table_no}"
            while i < len(lines):
                if TABLE_CLOSE_RE.search(lines[i]):
                    i += 1
                    break
                i += 1
            end = i
            tp = pages_in_segment(lines[start:end], current_page)
            sp = tp[0] if tp else current_page
            ep = tp[-1] if tp else sp
            spans.append(
                TableSpan(start_idx=start, end_idx=end, label=label, start_page=sp, end_page=ep)
            )
            continue
        i += 1
    return spans


def _table_chunk_start_line(content: list[str], span: TableSpan) -> int:
    """与 ``_table_chunk_lines`` 一致的起始行（用于避免表前导段重复输出）。"""
    start = span.start_idx
    for i in range(span.start_idx - 1, max(-1, span.start_idx - 10), -1):
        if TABLE_OPEN_RE.search(content[i]):
            break
        raw = content[i]
        body = strip_markdown_emphasis(line_body_after_page_marker(raw))
        if not body:
            start = i
            continue
        if _line_is_table_caption_only(raw) or page_no_from_line(raw) is not None:
            start = i
            continue
        if len(body) > 80 and "表格" not in body[:20]:
            break
        start = i
    return start


def _text_range_in_table_preamble(
    content: list[str], start_idx: int, end_idx: int, table: TableSpan
) -> bool:
    """表前短文本将并入该表文件，不再单独成块。"""
    if start_idx >= table.start_idx:
        return False
    chunk_start = _table_chunk_start_line(content, table)
    return start_idx >= chunk_start and end_idx <= table.start_idx


def _table_chunk_lines(content: list[str], span: TableSpan) -> list[str]:
    """表格块：含表前页码/表格题注/首段说明，保证上下文完整。"""
    start = _table_chunk_start_line(content, span)
    return content[start : span.end_idx]


def split_segment_chunks_with_tables(
    *,
    start_idx: int,
    end_idx: int,
    start_page: int,
    content: list[str],
    max_pages: int,
) -> list[dict]:
    """按页切分，且 HTML 表格整块独立为 ``-表N`` 文件，不从中间拆开。"""
    tables = [
        t
        for t in find_table_spans(content, start_page=start_page)
        if t.start_idx < end_idx and t.end_idx > start_idx
    ]
    if not tables:
        chunks = split_segment_chunks(
            start_idx=start_idx,
            end_idx=end_idx,
            start_page=start_page,
            content=content,
            max_pages=max_pages,
        )
        for c in chunks:
            c.setdefault("is_table", False)
        return chunks

    pieces: list[tuple[str, int, int, TableSpan | None]] = []
    pos = start_idx
    for table in tables:
        if pos < table.start_idx:
            if not _segment_is_table_filler(
                content, pos, table.start_idx
            ) and not _text_range_in_table_preamble(
                content, pos, table.start_idx, table
            ):
                pieces.append(("text", pos, table.start_idx, None))
        pieces.append(("table", table.start_idx, table.end_idx, table))
        pos = table.end_idx
    if pos < end_idx and not _segment_is_table_filler(content, pos, end_idx):
        pieces.append(("text", pos, end_idx, None))

    chunks: list[dict] = []
    text_n = 0
    table_n = 0
    for kind, a, b, span in pieces:
        if kind == "table":
            assert span is not None
            table_n += 1
            chunks.append({
                "suffix": f"-表{table_n}",
                "start_idx": a,
                "end_idx": b,
                "start_page": span.start_page,
                "end_page": span.end_page,
                "part_no": len(chunks) + 1,
                "parts": 0,
                "is_table": True,
                "table_label": span.label,
                "table_no": table_n,
            })
        else:
            subs = split_segment_chunks(
                start_idx=a,
                end_idx=b,
                start_page=start_page,
                content=content,
                max_pages=max_pages,
            )
            multi_file = bool(tables) or len(subs) > 1 or len(pieces) > 1
            for sub in subs:
                text_n += 1
                if not multi_file and len(subs) == 1:
                    sub["suffix"] = sub.get("suffix") or ""
                elif not sub.get("suffix"):
                    sub["suffix"] = f"-{text_n}"
                sub["is_table"] = False
                chunks.append(sub)

    total = len(chunks)
    for c in chunks:
        c["parts"] = total
    return chunks


def outlines_for_chunk(
    chunk: dict,
    chunk_lines: list[str],
) -> list[OutlineEntry]:
    if chunk.get("is_table"):
        label = chunk.get("table_label") or f"表{chunk.get('table_no', 1)}"
        return [OutlineEntry(label=label, page=chunk["start_page"])]
    return extract_outline_entries(chunk_lines, start_page=chunk["start_page"])


# ------------------ 正文渲染 ------------------
def clean_heading(level: int, line: str) -> str:
    title = normalize_title(re.sub(r"^#+\s*", "", line).strip())
    if is_chapter_title(title):
        return f"# {title}"
    if is_section_title(title):
        return f"## {title}"
    if is_article_or_subsection(title):
        return title
    lvl = min(max(level, 1), 4)
    return f"{'#' * lvl} {title}"


def render_body(lines: list[str]) -> str:
    rendered: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if page_no_from_line(line) is not None:
            rendered.append(line)
            continue
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            rendered.append(clean_heading(level, line))
        else:
            rendered.append(normalize_inline_ocr(line))

    compact: list[str] = []
    prev_blank = False
    for ln in rendered:
        blank = ln.strip() == ""
        if blank and prev_blank:
            continue
        compact.append(ln)
        prev_blank = blank
    return "\n".join(compact).strip() + "\n"


def article_label(topic: str) -> str:
    """从章节目录项提取条例编号标签（如 ``第一条``、``2.3.1``）。"""
    t = normalize_title(topic)
    am = ARTICLE_RE.match(t)
    if am:
        return am.group(0).strip()
    sm = SUBSECTION_NO_RE.match(t)
    if sm:
        return sm.group(1)
    head = t.split()[0] if t else t
    return head or t


# 指南 / 技术文件：``# 4.1 标题``、``4.1.1 正文条`` 等
GUIDE_NUM_TITLE_RE = re.compile(r"^(\d+(?:\.\d+)*)\s*\.?\s*(.*)$")
GUIDE_CLAUSE_LINE_RE = re.compile(
    r"^(\d+\.\d+(?:\.\d+)*)\s+[\u4e00-\u9fff（(]"
)


def _append_outline_entry(
    entries: list[OutlineEntry],
    seen: set[str],
    *,
    label: str,
    page: int,
) -> None:
    key = label.strip()
    if not key or key in seen:
        return
    seen.add(key)
    entries.append(OutlineEntry(label=key, page=page))


def _is_toc_noise_line(text: str) -> bool:
    """目录页常见 ``4.1 标题..............3``，不应进入条目录。"""
    return bool(re.search(r"\.{4,}", text))


def extract_outline_entries(lines: list[str], *, start_page: int = 1) -> list[OutlineEntry]:
    """抽取条号/小节编号（第X条、X.X.X、指南 4.1 等），仅用于索引定位。"""
    entries: list[OutlineEntry] = []
    seen: set[str] = set()
    current_page = start_page
    in_body = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if _is_toc_noise_line(stripped):
            continue

        current_page = advance_page_on_line(line, current_page)

        m = HEADING_RE.match(line)
        if m:
            title = normalize_title(m.group(2))
            if not in_body and (
                ARTICLE_RE.match(title)
                or CHAPTER_RE.match(title)
                or GUIDE_NUM_TITLE_RE.match(title)
            ):
                in_body = True
            if ARTICLE_RE.match(title):
                _append_outline_entry(
                    entries, seen, label=article_label(title), page=current_page
                )
                continue
            sm = SUBSECTION_NO_RE.match(title)
            if sm:
                _append_outline_entry(
                    entries, seen, label=sm.group(1), page=current_page
                )
                continue
            gm = GUIDE_NUM_TITLE_RE.match(title)
            if gm and gm.group(1):
                _append_outline_entry(
                    entries, seen, label=gm.group(1), page=current_page
                )
            continue

        body = line_body_after_page_marker(line)
        if not body or _is_toc_noise_line(body):
            continue
        if not in_body:
            if ARTICLE_RE.match(body) or GUIDE_CLAUSE_LINE_RE.match(body):
                in_body = True
            else:
                continue
        am = ARTICLE_RE.match(body)
        if am:
            _append_outline_entry(
                entries, seen, label=am.group(0).strip().rstrip(":："), page=current_page
            )
            continue
        sm = SUBSECTION_NO_RE.match(body)
        if sm:
            _append_outline_entry(entries, seen, label=sm.group(1), page=current_page)
            continue
        gm = GUIDE_CLAUSE_LINE_RE.match(body)
        if gm:
            _append_outline_entry(entries, seen, label=gm.group(1), page=current_page)

    return entries


def format_outline_catalog_line(entry: OutlineEntry) -> str:
    return f"  - {entry.label}（第{entry.page}页）"


def format_outline_summary_line(
    entry: OutlineEntry,
    *,
    todo: str = SUMMARY_TODO,
) -> str:
    return f"- **{entry.label}**（第{entry.page}页）：{todo}"


def format_file_summary_line(
    stem: str,
    start_page: int,
    end_page: int,
    *,
    todo: str = SUMMARY_TODO,
) -> str:
    if start_page == end_page:
        page_part = f"第{start_page}页"
    else:
        page_part = f"第{start_page}～{end_page}页"
    return f"- **{stem}**（{page_part}）：{todo}"


# ------------------ index 模板 ------------------
def build_file_index(
    title: str,
    catalog_lines: list[str],
    outlines: list[OutlineEntry],
    *,
    file_stem: str | None = None,
    start_page: int = 0,
    end_page: int = 0,
    file_summary_todo: str = SUMMARY_TODO,
) -> str:
    lines = [f"# {title}索引", "", "## 条目录", ""]
    lines.extend(catalog_lines)

    lines.extend(["", "## 条文摘要", ""])
    if outlines:
        for entry in outlines:
            lines.append(format_outline_summary_line(entry))
        if file_stem and start_page and file_summary_todo != SUMMARY_TODO:
            lines.append(
                format_file_summary_line(
                    file_stem,
                    start_page,
                    end_page or start_page,
                    todo=file_summary_todo,
                )
            )
    elif file_stem and start_page:
        lines.append(
            format_file_summary_line(
                file_stem,
                start_page,
                end_page or start_page,
                todo=file_summary_todo,
            )
        )
    else:
        lines.append(f"- **{title}**：{file_summary_todo}")

    return "\n".join(lines).rstrip() + "\n"


def build_root_index(
    src_name: str,
    chapters: list[Chapter],
    body_entries: list[BodyFileEntry],
    *,
    global_outlines: list[OutlineEntry] | None = None,
) -> str:
    lines = [f"# {src_name}索引", ""]

    if chapters:
        lines.extend(["", "## 章节目录", ""])
        for chap in chapters:
            lines.append(f"- {catalog_title(chap.title)}")
            for sec in chap.sections:
                lines.append(f"  - {catalog_title(sec.title)}")
    elif global_outlines:
        lines.extend(["", "## 条目录", ""])
        for entry in global_outlines:
            lines.append(format_outline_catalog_line(entry).lstrip())
    else:
        lines.extend(["", "## 章节目录", ""])

    if body_entries and any(e.start_page for e in body_entries):
        lines.extend(["", "## 文件目录", ""])
        for entry in body_entries:
            stem = Path(entry.rel_path).stem
            sp, ep = entry.start_page, entry.end_page or entry.start_page
            if sp == ep:
                lines.append(f"- {stem}（第{sp}页）")
            else:
                lines.append(f"- {stem}（第{sp}～{ep}页）")

    lines.extend(["", "## 各文件摘要", ""])
    if body_entries:
        for entry in body_entries:
            stem = Path(entry.rel_path).stem
            sp = entry.start_page or 0
            ep = entry.end_page or sp
            if sp:
                lines.append(format_file_summary_line(stem, sp, ep))
            else:
                lines.append(f"- **{catalog_title(entry.title)}**：{SUMMARY_TODO}")
    else:
        lines.append(f"- {SUMMARY_TODO}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _section_group_body_metrics(
    content: list[str], sections: list[Section]
) -> tuple[int, int]:
    start, end = sections[0].start_idx, sections[-1].end_idx
    non_empty = 0
    chars = 0
    for line in content[start:end]:
        if page_no_from_line(line) is not None:
            body = line_body_after_page_marker(line).strip()
            if not body:
                continue
            line = body
        if not line.strip():
            continue
        non_empty += 1
        chars += len(line)
    return non_empty, chars


def _section_is_small(content: list[str], sec: Section) -> bool:
    nl, nc = _section_group_body_metrics(content, [sec])
    return nl < MIN_SECTION_BODY_LINES or nc < MIN_SECTION_BODY_CHARS


def plan_section_groups(
    sections: list[Section],
    content: list[str],
) -> list[SectionGroup]:
    """将过小的连续二级节合并；合并块使用 ``TODO-待LLM命名-N`` 文件名。"""
    if not sections:
        return []

    groups: list[SectionGroup] = []
    buf: list[Section] = []
    merge_no = 0

    def flush_buffer() -> None:
        nonlocal merge_no, buf
        if not buf:
            return
        if len(buf) == 1:
            sec = buf[0]
            groups.append(
                SectionGroup(
                    sections=[sec],
                    merged=False,
                    file_stem=section_file_stem(sec.title),
                )
            )
        else:
            merge_no += 1
            groups.append(
                SectionGroup(
                    sections=list(buf),
                    merged=True,
                    file_stem=f"{MERGED_NAME_TODO_PREFIX}-{merge_no}",
                )
            )
        buf = []

    for sec in sections:
        if _section_is_small(content, sec):
            buf.append(sec)
            bn, bc = _section_group_body_metrics(content, buf)
            if len(buf) >= MAX_MERGED_SECTIONS or (
                bn >= MIN_SECTION_BODY_LINES and bc >= MIN_SECTION_BODY_CHARS
            ):
                flush_buffer()
        else:
            flush_buffer()
            groups.append(
                SectionGroup(
                    sections=[sec],
                    merged=False,
                    file_stem=section_file_stem(sec.title),
                )
            )
    flush_buffer()
    return groups


def build_file_catalog_lines(
    *,
    chapter_title: str,
    section_title: str | None = None,
    outlines: list[OutlineEntry] | None = None,
    extra_lines: list[str] | None = None,
    merged_section_titles: list[str] | None = None,
) -> list[str]:
    current_title = catalog_title(section_title or chapter_title)
    lines = [f"- {current_title}"]
    if merged_section_titles:
        lines.append("  - （合并块，含以下二级标题）")
        for t in merged_section_titles:
            lines.append(f"    - {catalog_title(t)}")
        lines.append(f"  - **文件名**：{NAME_TODO}")
    if outlines:
        for entry in outlines:
            lines.append(format_outline_catalog_line(entry))
    if extra_lines:
        lines.extend(extra_lines)
    return lines


def build_indexes_flat_document(
    src_name: str,
    out_root: Path,
    content: list[str],
    max_pages: int,
) -> tuple[int, list[BodyFileEntry]]:
    """无「第X章」的指南 / 规范性文件：按页拆成多个正文文件。"""
    n_body_files = 0
    body_entries: list[BodyFileEntry] = []
    stem = slug_title(src_name)
    global_outlines = extract_outline_entries(content, start_page=1)

    chunks = split_segment_chunks_with_tables(
        start_idx=0,
        end_idx=len(content),
        start_page=1,
        content=content,
        max_pages=max_pages,
    )

    for chunk in chunks:
        if chunk.get("is_table"):
            span = TableSpan(
                start_idx=chunk["start_idx"],
                end_idx=chunk["end_idx"],
                label=chunk.get("table_label") or "表",
                start_page=chunk["start_page"],
                end_page=chunk["end_page"],
            )
            chunk_lines = _table_chunk_lines(content, span)
        else:
            chunk_lines = content[chunk["start_idx"] : chunk["end_idx"]]
        file_stem = stem + chunk["suffix"]
        write_text(out_root / f"{file_stem}.md", render_body(chunk_lines))
        n_body_files += 1

        outlines = outlines_for_chunk(chunk, chunk_lines)
        extra: list[str] = []
        if chunk["parts"] > 1:
            extra.append(
                f"- 拆分段：第 {chunk['part_no']} 段，共 {chunk['parts']} 段"
            )

        catalog = [f"- {catalog_title(src_name)}"]
        if outlines:
            catalog.extend(format_outline_catalog_line(e) for e in outlines)
        elif chunk["parts"] > 1:
            catalog.append(
                f"  - （本段无独立条号）（第{chunk['start_page']}页）"
            )
        catalog.extend(extra)

        write_text(
            out_root / f"{file_stem}-index.md",
            build_file_index(
                file_stem,
                catalog,
                outlines,
                file_stem=file_stem,
                start_page=chunk["start_page"],
                end_page=chunk["end_page"],
            ),
        )
        body_entries.append(
            BodyFileEntry(
                title=src_name + chunk["suffix"],
                rel_path=f"{file_stem}.md",
                start_page=chunk["start_page"],
                end_page=chunk["end_page"],
            )
        )

    write_text(
        out_root / f"{src_name}-index.md",
        build_root_index(
            src_name, [], body_entries, global_outlines=global_outlines
        ),
    )
    return n_body_files, body_entries


def _write_body_chunk(
    *,
    chap_dir: Path,
    stem: str,
    chunk_lines: list[str],
    heading_line: str | None,
    part_no: int,
) -> list[str]:
    if part_no > 1 and heading_line:
        chunk_lines = [heading_line, ""] + chunk_lines
    write_text(chap_dir / f"{stem}.md", render_body(chunk_lines))
    return chunk_lines


def _process_chunk_range(
    *,
    content: list[str],
    start_idx: int,
    end_idx: int,
    start_page: int,
    max_pages: int,
    out_dir: Path,
    file_title: str,
    file_stem: str,
    heading_line: str | None,
    catalog_chapter: str,
    catalog_section: str | None,
    body_entries: list[BodyFileEntry],
    rel_dir: str,
    merged_section_titles: list[str] | None = None,
    summary_todo: str = SUMMARY_TODO,
) -> tuple[int, list[BodyFileEntry]]:
    """写一段区间内的全部 chunk（含表格独立文件）及对应 index。"""
    n = 0
    for chunk in split_segment_chunks_with_tables(
        start_idx=start_idx,
        end_idx=end_idx,
        start_page=start_page,
        content=content,
        max_pages=max_pages,
    ):
        if chunk.get("is_table"):
            span = TableSpan(
                start_idx=chunk["start_idx"],
                end_idx=chunk["end_idx"],
                label=chunk.get("table_label") or "表",
                start_page=chunk["start_page"],
                end_page=chunk["end_page"],
            )
            chunk_lines = _table_chunk_lines(content, span)
        else:
            chunk_lines = content[chunk["start_idx"] : chunk["end_idx"]]
        stem = file_stem + chunk["suffix"]
        if chunk.get("is_table"):
            write_text(out_dir / f"{stem}.md", render_body(chunk_lines))
        else:
            chunk_lines = _write_body_chunk(
                chap_dir=out_dir,
                stem=stem,
                chunk_lines=chunk_lines,
                heading_line=heading_line,
                part_no=chunk["part_no"],
            )
        n += 1
        body_entries.append(
            BodyFileEntry(
                title=file_title + chunk["suffix"],
                rel_path=f"{rel_dir}/{stem}.md",
                start_page=chunk["start_page"],
                end_page=chunk["end_page"],
            )
        )
        extra: list[str] = []
        if chunk["parts"] > 1:
            extra.append(
                f"- 拆分段：第 {chunk['part_no']} 段，共 {chunk['parts']} 段"
            )
        outlines = outlines_for_chunk(chunk, chunk_lines)
        catalog_lines = build_file_catalog_lines(
            chapter_title=catalog_chapter,
            section_title=catalog_section,
            outlines=outlines,
            extra_lines=extra,
            merged_section_titles=merged_section_titles,
        )
        write_text(
            out_dir / f"{stem}-index.md",
            build_file_index(
                stem,
                catalog_lines,
                outlines,
                file_stem=stem,
                start_page=chunk["start_page"],
                end_page=chunk["end_page"],
                file_summary_todo=summary_todo,
            ),
        )
    return n, body_entries


# ------------------ 写文件 ------------------
def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ------------------ 主流程 ------------------
def build_indexes(
    src_name: str,
    out_root: Path,
    content: list[str],
    chapters: list[Chapter],
    max_pages: int,
) -> tuple[int, list[BodyFileEntry]]:
    n_body_files = 0
    body_entries: list[BodyFileEntry] = []

    if not chapters:
        return build_indexes_flat_document(
            src_name, out_root, content, max_pages
        )

    for chap in chapters:
        chap_title = normalize_title(chap.title)
        chap_slug = chapter_dir_slug(chap_title)
        chap_dir = out_root / chap_slug
        chap_dir.mkdir(parents=True, exist_ok=True)
        chap_heading = content[chap.start_idx]
        chap_stem = chapter_body_stem(chap_title)

        rel_dir = chap_slug
        if not chap.sections:
            added, body_entries = _process_chunk_range(
                content=content,
                start_idx=chap.start_idx,
                end_idx=chap.end_idx,
                start_page=chap.start_page,
                max_pages=max_pages,
                out_dir=chap_dir,
                file_title=chap_title,
                file_stem=chap_stem,
                heading_line=chap_heading,
                catalog_chapter=chap_title,
                catalog_section=None,
                body_entries=body_entries,
                rel_dir=rel_dir,
            )
            n_body_files += added
            continue

        if chap.sections[0].start_idx > chap.start_idx:
            added, body_entries = _process_chunk_range(
                content=content,
                start_idx=chap.start_idx,
                end_idx=chap.sections[0].start_idx,
                start_page=chap.start_page,
                max_pages=max_pages,
                out_dir=chap_dir,
                file_title=chap_title,
                file_stem=chap_stem,
                heading_line=chap_heading,
                catalog_chapter=chap_title,
                catalog_section=None,
                body_entries=body_entries,
                rel_dir=rel_dir,
            )
            n_body_files += added

        for grp in plan_section_groups(chap.sections, content):
            start_idx = grp.sections[0].start_idx
            end_idx = grp.sections[-1].end_idx
            start_page = grp.sections[0].start_page
            heading_line = content[start_idx]
            merged_titles = (
                [normalize_title(s.title) for s in grp.sections]
                if grp.merged
                else None
            )
            if grp.merged:
                file_title = grp.file_stem
                catalog_section = None
            else:
                file_title = normalize_title(grp.sections[0].title)
                catalog_section = file_title
            added, body_entries = _process_chunk_range(
                content=content,
                start_idx=start_idx,
                end_idx=end_idx,
                start_page=start_page,
                max_pages=max_pages,
                out_dir=chap_dir,
                file_title=file_title,
                file_stem=grp.file_stem,
                heading_line=heading_line,
                catalog_chapter=chap_title,
                catalog_section=catalog_section,
                body_entries=body_entries,
                rel_dir=rel_dir,
                merged_section_titles=merged_titles,
                summary_todo=NAME_TODO if grp.merged else SUMMARY_TODO,
            )
            n_body_files += added

    write_text(
        out_root / f"{src_name}-index.md",
        build_root_index(src_name, chapters, body_entries),
    )
    return n_body_files, body_entries


@dataclass
class SplitWikiResult:
    source: Path
    output_root: Path
    chapter_count: int
    body_file_count: int
    src_line_count: int
    body_line_count: int


def split_wiki_file(
    source: Path | str,
    *,
    output_root: Path | str = "wiki",
    max_pages_per_section: int = 2,
) -> SplitWikiResult:
    source = Path(source).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"源文件不存在：{source}")

    lines = source.read_text(encoding="utf-8").splitlines()
    src_line_count = len(lines)
    src_stem = source.stem
    chapters, start_idx = parse_structure(lines, src_stem)
    content_lines = lines[start_idx:] if start_idx is not None else lines

    if chapters:
        offset = start_idx or 0
        adjusted: list[Chapter] = []
        for chap in chapters:
            new_chap = Chapter(
                title=chap.title,
                start_idx=chap.start_idx - offset,
                start_page=chap.start_page,
                end_idx=chap.end_idx - offset,
            )
            for sec in chap.sections:
                new_chap.sections.append(
                    Section(
                        title=sec.title,
                        start_idx=sec.start_idx - offset,
                        start_page=sec.start_page,
                        end_idx=sec.end_idx - offset,
                    )
                )
            adjusted.append(new_chap)
        chapters = adjusted

    spec_name = source.stem
    out_root = Path(output_root).resolve() / spec_name

    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    n_body, _ = build_indexes(
        spec_name,
        out_root,
        content_lines,
        chapters,
        max_pages_per_section,
    )

    body_files = [p for p in out_root.rglob("*.md") if not p.name.endswith("-index.md")]
    body_line_count = sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in body_files
    )

    return SplitWikiResult(
        source=source,
        output_root=out_root,
        chapter_count=len(chapters),
        body_file_count=n_body,
        src_line_count=src_line_count,
        body_line_count=body_line_count,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 raw/<规范名>.md 按章 / 节拆分到 wiki/<规范名>/"
    )
    parser.add_argument("source", help="源 markdown 文件，例如 raw/药物警戒质量管理规范.md")
    parser.add_argument(
        "--output-root",
        default="wiki",
        help="wiki 输出根目录（默认 wiki）",
    )
    parser.add_argument(
        "--max-pages-per-section",
        type=int,
        default=2,
        help="一个章/节最多多少页；超过则按 -1/-2/... 继续切（默认 2，同 ccic 以二级标题为文件单元）",
    )
    args = parser.parse_args()

    result = split_wiki_file(
        args.source,
        output_root=args.output_root,
        max_pages_per_section=args.max_pages_per_section,
    )

    print(f"输出目录：{result.output_root}")
    print(f"章数：{result.chapter_count}")
    print(f"正文文件数：{result.body_file_count}")


if __name__ == "__main__":
    main()
