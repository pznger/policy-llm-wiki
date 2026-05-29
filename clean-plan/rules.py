"""本地（确定性）清洗规则。

LLM 调用前先跑一遍：把能用代码确定性修掉的低风险问题修掉，省 token、避免漂移。

修复内容：
- 删除独立成行的页脚页码（纯数字 / 形如 ``- 12 -`` / ``— 12 —`` / ``· 12 ·`` / ``—— 12 ——``）
- 删除 PDF 页眉页脚：发布机关行（如 ``国家市场监督管理总局、国家卫生健康委员会发布``）、重复出现的 ``XX总局规章``、OCR 残留单字 ``X``
- 从正文中剥离被硬换行/合并进来的上述页眉页脚片段
- 删除中文条款编号上的多余空格（``第 四 条`` -> ``第四条``、``第二十 一 条`` -> ``第二十一条``）
- 标题规范化：仅 ``第X章`` 为一级 ``#``；``第X节`` 或 ``X.X`` 为二级 ``##``；``第X条`` / ``X.X.X`` / 误标的 ``###`` ``####`` 降为正文
- 删除中文一级 / 二级标题中"中文之间的多余空格"（``# 第一章 总 则`` -> ``# 第一章 总则``）
- 移除中文条款 / 章 / 节编号上的错误加粗（``**第三十七条**`` -> ``第三十七条``）
- 移除单字符 / 单数字加粗（``**6**`` -> ``6``）
- 移除两个中文字符之间被错误插入的单空格（``生产 企业`` -> ``生产企业``）
- 合并 PDF 按行宽产生的硬换行，恢复自然段落；标题、列表、表格、图片、页码注释不合并
- 压缩多余空行（连续 3+ 空行 -> 2 行）

不会改动：
- HTML 表格（``<table ...>...</table>``）
- 图片（``![...](...)``）
- HTML 注释（含我们插入的 ``<!-- 第 N 页 -->``）
- 行内 / 块级 LaTeX 公式 (``$...$`` / ``$$...$$``)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 在修复"中文之间多余空格"前，先把这些区域整体替换为占位符避免误伤
_PROTECTED_REGEX = re.compile(
    r"(<table\b[^>]*>[\s\S]*?</table>)"
    r"|(!\[[^\]]*\]\([^)]*\)(?:\s*<!--[^>]*-->)?)"
    r"|(<!--[\s\S]*?-->)"
    r"|(\$\$[\s\S]*?\$\$)"
    r"|(\$[^\n$]+?\$)",
    re.IGNORECASE,
)

_NUMERIC_FOOTER = re.compile(r"^[ \t]*[-—·\.]?\s*\d{1,4}\s*[-—·\.]?[ \t]*$")
# "附录 -1-" / " - 12 - " 这类
_DASH_PAGE = re.compile(r"^[ \t]*[-—·]\s*\d{1,4}\s*[-—·][ \t]*$")
# "—— 12 ——"（药物警戒等部委文件常见）
_EM_DASH_PAGE = re.compile(r"^[ \t]*——\s*\d{1,4}\s*——[ \t]*$")
# 页脚：国家市场监督管理总局、国家卫生健康委员会发布
_PUBLISH_LINE = re.compile(
    r"^[ \t]*(?:[Xx])?"
    r"(?:原)?国家[\u4e00-\u9fff、]{2,100}?"
    r"(?:总局|委员会|部|署)"
    r"(?:[、,，][\u4e00-\u9fff、]{2,60}?(?:总局|委员会|部|署))*"
    r"发布[ \t]*$"
)
# 页眉：国家市场监督管理总局规章
_REG_HEADER_LINE = re.compile(
    r"^[ \t]*(?:原)?国家(?:市场监督管理|食品药品监督管理|药品监督管理)总局规章[ \t]*$"
)
_OCR_JUNK_LINE = re.compile(r"^[ \t]*[Xx][ \t]*$")
# 被合并进段落的页眉页脚片段（不要求独立成行）
_INLINE_REG_HEADER = re.compile(
    r"(?:原)?国家(?:市场监督管理|食品药品监督管理|药品监督管理)总局规章"
)
# 有界匹配，避免在长段落上灾难性回溯
_INLINE_PUBLISH = re.compile(
    r"(?:[Xx](?=国)|(?<=[\u4e00-\u9fff])[Xx])?"
    r"(?:原)?国家[\u4e00-\u9fff、]{4,56}?(?:总局|委员会|部|署)"
    r"(?:[、,，]国家[\u4e00-\u9fff、]{4,40}?(?:总局|委员会|部|署))?"
    r"发布"
)
# 中文条款编号正则（支持一/二/.../二十/二十一 等）
_CN_NUM = "一二三四五六七八九十百零〇两"
_ARTICLE_PAT = re.compile(rf"第\s*([{_CN_NUM}\d]+(?:\s+[{_CN_NUM}\d]+)*)\s*条")
_CHAPTER_PAT = re.compile(rf"第\s*([{_CN_NUM}\d]+(?:\s+[{_CN_NUM}\d]+)*)\s*章")
_SECTION_PAT = re.compile(rf"第\s*([{_CN_NUM}\d]+(?:\s+[{_CN_NUM}\d]+)*)\s*节")
_ITEM_PAT = re.compile(rf"第\s*([{_CN_NUM}\d]+(?:\s+[{_CN_NUM}\d]+)*)\s*项")

# 中文之间被错误插入的单空格（中文 + 空白 + 中文）
_CN_GAP = re.compile(r"([\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff、，。；：！？）」』】])")
_CN_GAP2 = re.compile(r"([\u4e00-\u9fff、，。；：！？（「『【])[ \t]+(?=[\u4e00-\u9fff])")

# 单字符 / 单数字加粗
_BOLD_SHORT = re.compile(r"\*\*\s*([\u4e00-\u9fff\w]{1,2})\s*\*\*")

# Markdown 标题前缀
_HEADING_MARKERS = re.compile(r"^#{1,6}\s+")
# 两级数字编号（2.3），非三级（2.3.1）
_NUM_SECTION2 = re.compile(r"^\d+\.\d+(?:\s|[^\d.]|$)")
_NUM_SECTION3 = re.compile(r"^\d+\.\d+\.\d+")
_PROTECTED_TOKEN = re.compile(r"^\x00P\d+\x00$")
_LIST_START = re.compile(
    r"^(\(?[一二三四五六七八九十百零〇]+\)|（[一二三四五六七八九十百零〇]+）|"
    r"\d+[\.、)]|[（(]\d+[）)])"
)
_ARTICLE_START = re.compile(rf"^第[{_CN_NUM}\d]+条")
_SENTENCE_END = re.compile(r"[。！？；;]$")
_PARAGRAPH_END = re.compile(r"[。！？；;：:]$")


@dataclass
class RuleStats:
    """逐项统计本地规则的命中次数。"""
    page_numbers_removed: int = 0
    boilerplate_removed: int = 0
    article_spaces_fixed: int = 0
    chapter_spaces_fixed: int = 0
    section_spaces_fixed: int = 0
    bold_short_removed: int = 0
    bold_article_removed: int = 0
    cn_inline_space_fixed: int = 0
    blank_lines_compressed: int = 0
    heading_h1_fixed: int = 0
    heading_h2_fixed: int = 0
    heading_demoted: int = 0
    linebreaks_merged: int = 0

    @property
    def total_changes(self) -> int:
        return (
            self.page_numbers_removed
            + self.boilerplate_removed
            + self.article_spaces_fixed
            + self.chapter_spaces_fixed
            + self.section_spaces_fixed
            + self.bold_short_removed
            + self.bold_article_removed
            + self.cn_inline_space_fixed
            + self.blank_lines_compressed
            + self.heading_h1_fixed
            + self.heading_h2_fixed
            + self.heading_demoted
            + self.linebreaks_merged
        )


def _normalize_cn_token(s: str) -> str:
    """``第 四 条`` -> ``第四条``。"""
    return re.sub(r"\s+", "", s)


def _fix_article_like(line: str, stats: RuleStats) -> str:
    new_line, n = _ARTICLE_PAT.subn(
        lambda m: "第" + _normalize_cn_token(m.group(1)) + "条", line
    )
    if n:
        stats.article_spaces_fixed += n
    line = new_line

    new_line, n = _CHAPTER_PAT.subn(
        lambda m: "第" + _normalize_cn_token(m.group(1)) + "章", line
    )
    if n:
        stats.chapter_spaces_fixed += n
    line = new_line

    new_line, n = _SECTION_PAT.subn(
        lambda m: "第" + _normalize_cn_token(m.group(1)) + "节", line
    )
    if n:
        stats.section_spaces_fixed += n
    line = new_line

    new_line, n = _ITEM_PAT.subn(
        lambda m: "第" + _normalize_cn_token(m.group(1)) + "项", line
    )
    if n:
        stats.section_spaces_fixed += n
    return new_line


def _strip_bold_on_legal_tokens(line: str, stats: RuleStats) -> str:
    """``**第三十七条**`` / ``**第一章 总则**`` -> 去掉两端的 **"""
    pat = re.compile(rf"\*\*\s*(第[{_CN_NUM}\d]+[条章节项][^*]*?)\s*\*\*")
    new, n = pat.subn(lambda m: m.group(1).strip(), line)
    if n:
        stats.bold_article_removed += n
    return new


def _strip_heading_marks(line: str) -> str:
    return _HEADING_MARKERS.sub("", line.strip())


def _is_chapter_title(body: str) -> bool:
    return bool(_CHAPTER_PAT.match(body.strip()))


def _is_section_title(body: str) -> bool:
    s = body.strip()
    if _SECTION_PAT.match(s):
        return True
    if _NUM_SECTION3.match(s):
        return False
    return bool(_NUM_SECTION2.match(s))


def _normalize_heading_line(line: str, stats: RuleStats) -> str:
    """仅保留一级（章）、二级（节 / X.X）；其余去掉 # 当正文。"""
    stripped = line.strip()
    if not stripped:
        return line
    had_hash = bool(_HEADING_MARKERS.match(stripped))
    body = _strip_heading_marks(stripped)

    if _is_chapter_title(body):
        stats.heading_h1_fixed += 1
        return "# " + body.strip()

    if _is_section_title(body):
        stats.heading_h2_fixed += 1
        return "## " + body.strip()

    if had_hash:
        stats.heading_demoted += 1
        return body.strip()

    return line


def _strip_bold_short(line: str, stats: RuleStats) -> str:
    """``**6**`` / ``**A**`` -> 去掉两端的 **"""
    new, n = _BOLD_SHORT.subn(lambda m: m.group(1), line)
    if n:
        stats.bold_short_removed += n
    return new


def _fix_cn_inline_spaces(text: str, stats: RuleStats) -> str:
    """处理"中文 + 空格 + 中文/中文标点"被错误插入空格的情况。"""
    before = text
    text = _CN_GAP.sub(r"\1", text)
    text = _CN_GAP2.sub(r"\1", text)
    if text != before:
        # 大致估算修复了多少处（按差异长度）
        stats.cn_inline_space_fixed += max(1, (len(before) - len(text)))
    return text


def _is_boundary_line(line: str) -> bool:
    """不能与前后合并的独立结构。"""
    s = line.strip()
    if not s:
        return True
    return (
        bool(_PROTECTED_TOKEN.match(s))
        or s.startswith("#")
        or s.startswith("<table")
        or s.startswith("</table")
        or s.startswith("<tr")
        or s.startswith("<td")
        or s.startswith("![")
        or s.startswith("<!--")
    )


def _is_new_paragraph_start(line: str) -> bool:
    """明显应另起一段的行首模式。"""
    s = _strip_heading_marks(line.strip())
    if not s:
        return True
    return (
        _is_chapter_title(s)
        or _is_section_title(s)
        or bool(_ARTICLE_START.match(s))
        or bool(_LIST_START.match(s))
    )


def _should_merge_lines(prev: str, cur: str, saw_blank: bool) -> bool:
    """判断 cur 是否只是 prev 的 PDF 软换行延续。"""
    prev = prev.strip()
    cur = cur.strip()
    if not prev or not cur:
        return False
    if _is_boundary_line(prev) or _is_boundary_line(cur):
        return False
    if _is_new_paragraph_start(cur):
        return False
    # 空行后的完整句子通常是真段落边界；未完句、逗号/顿号结尾则继续合并。
    if saw_blank and _PARAGRAPH_END.search(prev):
        return False
    # 即使没有空行，句号/分号结尾后接新句，也更可能是段落边界。
    if not saw_blank and _SENTENCE_END.search(prev):
        return False
    return True


def _join_wrapped_lines(prev: str, cur: str) -> str:
    """合并两行：中文之间不加空格，英文/数字之间保留一个空格。"""
    prev = prev.rstrip()
    cur = cur.lstrip()
    if not prev:
        return cur
    if not cur:
        return prev
    if re.search(r"[A-Za-z0-9]$", prev) and re.match(r"[A-Za-z0-9]", cur):
        return prev + " " + cur
    return prev + cur


def _reflow_pdf_linebreaks(text: str, stats: RuleStats) -> str:
    """把 PDF 按视觉行宽产生的断行合并为自然段落。"""
    lines = text.split("\n")
    out: list[str] = []
    current = ""
    saw_blank = False

    def flush_current() -> None:
        nonlocal current
        if current:
            out.append(current)
            current = ""

    for raw in lines:
        line = raw.strip()
        if not line:
            saw_blank = True
            continue

        if _is_boundary_line(line):
            flush_current()
            if out and out[-1] != "":
                out.append("")
            out.append(line)
            out.append("")
            saw_blank = False
            continue

        if current and _should_merge_lines(current, line, saw_blank):
            current = _join_wrapped_lines(current, line)
            stats.linebreaks_merged += 1
        else:
            flush_current()
            if saw_blank and out and out[-1] != "":
                out.append("")
            current = line
        saw_blank = False

    flush_current()
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def _is_boilerplate_line(s: str) -> bool:
    return bool(
        _NUMERIC_FOOTER.match(s)
        or _DASH_PAGE.match(s)
        or _EM_DASH_PAGE.match(s)
        or _PUBLISH_LINE.match(s)
        or _REG_HEADER_LINE.match(s)
        or _OCR_JUNK_LINE.match(s)
    )


def _strip_inline_boilerplate(line: str, stats: RuleStats) -> str:
    for pat in (_INLINE_REG_HEADER, _INLINE_PUBLISH):
        line, n = pat.subn("", line)
        if n:
            stats.boilerplate_removed += n
    return line


def _remove_page_footers(text: str, stats: RuleStats) -> str:
    out: list[str] = []
    for ln in text.split("\n"):
        s = ln.strip()
        if _is_boilerplate_line(s):
            if _NUMERIC_FOOTER.match(s) or _DASH_PAGE.match(s) or _EM_DASH_PAGE.match(s):
                stats.page_numbers_removed += 1
            else:
                stats.boilerplate_removed += 1
            continue
        out.append(ln)
    return "\n".join(out)


def _compress_blank_lines(text: str, stats: RuleStats) -> str:
    before = text.count("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    after = text.count("\n")
    if after < before:
        stats.blank_lines_compressed += before - after
    return text


def _protect_and_run(text: str, fn) -> str:
    """先用占位符保护掉表格 / 图片 / HTML注释 / 公式，再跑 fn，再还原。"""
    protected: list[str] = []

    def _save(match: re.Match) -> str:
        protected.append(match.group(0))
        return f"\x00P{len(protected) - 1}\x00"

    masked = _PROTECTED_REGEX.sub(_save, text)
    processed = fn(masked)

    def _restore(match: re.Match) -> str:
        return protected[int(match.group(1))]

    return re.sub(r"\x00P(\d+)\x00", _restore, processed)


def apply_local_rules(text: str) -> tuple[str, RuleStats]:
    """对整篇 markdown 跑本地规则；返回 (清洗后的 markdown, 统计)。"""
    stats = RuleStats()

    # 1) 去掉页脚（在保护层外执行：纯数字 / -N- 整行不会出现在表格 / 注释里）
    text = _remove_page_footers(text, stats)

    # 2) 中文空格修复 + 加粗修复：放在保护层内运行
    def _line_level(masked: str) -> str:
        new_lines: list[str] = []
        for ln in masked.split("\n"):
            ln = _fix_article_like(ln, stats)
            ln = _strip_bold_on_legal_tokens(ln, stats)
            ln = _strip_bold_short(ln, stats)
            ln = _normalize_heading_line(ln, stats)
            ln = _strip_inline_boilerplate(ln, stats)
            new_lines.append(ln)
        whole = "\n".join(new_lines)
        whole = _fix_cn_inline_spaces(whole, stats)
        whole = _reflow_pdf_linebreaks(whole, stats)
        return whole

    text = _protect_and_run(text, _line_level)

    # 2b) 段落合并后可能再次露出页眉页脚，再扫一遍行内片段
    text = "\n".join(_strip_inline_boilerplate(ln, stats) for ln in text.split("\n"))

    # 3) 收尾：压缩多余空行
    text = _compress_blank_lines(text, stats)

    return text, stats
