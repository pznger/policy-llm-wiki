"""PDF 提取器：仅使用 pdfplumber，表格/图片交给多模态模型恢复。

处理策略：
1. 文本：pdfplumber 提取页面文字，并尽量排除表格/图片区域，减少重复内容。
2. 表格：用 pdfplumber 定位表格 bbox，将表格截图保存到 assets，再调用多模态模型恢复为 HTML table。
3. 图片：用 pdfplumber 定位图片 bbox，将图片区域截图保存到 assets，再调用多模态模型生成中文说明。
4. 页码：每页末尾追加 ``<!-- 第 N 页 -->``，方便后续溯源。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import pdfplumber
from loguru import logger

from config import (
    IMAGE_AREA_RATIO_FOR_VLM,
    TABLE_MAX_AREA_RATIO,
    TABLE_MAX_AVG_CELL_CHARS,
    TABLE_MAX_CELL_CHARS,
    TABLE_MAX_COL_VAR_RATIO,
    TABLE_MIN_AREA_RATIO,
    TABLE_MIN_COLS,
    TABLE_MIN_FILLED_RATIO,
    TABLE_MIN_ROWS,
    TABLE_VLM_CLASSIFY,
)
from extractors.multimodal_client import MultiModalClient
from utils.markdown_utils import page_marker, paragraphs_to_md, pdfplumber_table_to_html


TABLE_PROMPT = """请把图片中的表格完整恢复为 HTML 表格。

要求：
- 只输出一个 `<table border="1" >...</table>`，不要解释。
- 单元格使用 `<td>`，表头也使用 `<td>`。
- 尽量保留合并单元格；可使用 `rowspan` / `colspan`。
- 单元格内换行用 `<br>`。
- 不要输出 Markdown 表格。
- 如果图片不是表格或无法识别，输出空字符串。
"""

IMAGE_PROMPT = """请用简体中文客观描述这张政策文档中的图片/图示。

要求：
- 如果是流程图、示意图、印章、附件截图，请描述关键文字和结构。
- 如果是装饰性图标、空白或无意义内容，请直接回复「（无关图片）」。
- 控制在 150 字以内。
"""


@dataclass
class PageResult:
    page_no: int
    tier: int
    markdown: str
    images: list[str]
    n_tables: int = 0
    n_chars: int = 0
    vlm_calls: int = 0
    fallback_reason: str = ""


@dataclass
class PdfStats:
    """单个 PDF 文档的提取统计。"""

    file: str = ""
    total_pages: int = 0
    tier_pages: dict[int, int] | None = None
    fallback_reasons: list[str] | None = None
    total_tables: int = 0
    total_images: int = 0
    vlm_attempted: int = 0
    vlm_success: int = 0
    vlm_failed: int = 0
    vlm_enabled: bool = False
    vlm_model: str = ""
    elapsed_sec: float = 0.0

    def __post_init__(self) -> None:
        if self.tier_pages is None:
            # 保持 main.py 报告兼容：现在只有 Tier1(pdfplumber) 会命中。
            self.tier_pages = {1: 0, 2: 0, 3: 0}
        if self.fallback_reasons is None:
            self.fallback_reasons = []


def extract_pdf(
    pdf_path: Path,
    raw_dir: Path,
    use_vlm: bool = True,
    force_tier: int | None = None,
) -> tuple[str, PdfStats]:
    """从 PDF 提取并返回 (markdown 字符串, 统计信息)。

    ``force_tier`` 参数保留是为了兼容 main.py，但当前实现只使用 pdfplumber。
    表格与图片需要多模态模型；若 ``use_vlm=False``，表格会回退到 pdfplumber 的原始表格抽取。
    """
    pdf_path = Path(pdf_path)
    stem = pdf_path.stem
    assets_dir = raw_dir / f"{stem}_assets"

    with pdfplumber.open(str(pdf_path)) as pdf:
        total_pages = len(pdf.pages)

    vlm = MultiModalClient() if use_vlm else None
    stats = PdfStats(
        file=pdf_path.name,
        total_pages=total_pages,
        vlm_enabled=use_vlm,
        vlm_model=(vlm.model if vlm else ""),
    )

    if force_tier is not None and force_tier != 1:
        logger.warning(
            f"[PDF] 当前版本仅使用 pdfplumber，忽略 force_tier={force_tier}"
        )

    logger.info(
        f"[PDF] 开始 {pdf_path.name} | 共 {total_pages} 页 | "
        f"parser=pdfplumber-only | VLM={'on(' + (vlm.model if vlm else '') + ')' if use_vlm else 'off'}"
    )

    t0 = time.time()
    md_parts: list[str] = [f"# {stem}", ""]

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_idx, page in enumerate(pdf.pages, 1):
            result = _extract_one_page(
                page=page,
                page_no=page_idx,
                assets_dir=assets_dir,
                doc_stem=stem,
                vlm=vlm,
            )

            stats.tier_pages[1] += 1
            stats.total_tables += result.n_tables
            stats.total_images += len(result.images)
            stats.vlm_attempted += result.vlm_calls

            logger.info(
                f"[PDF] {pdf_path.name} p{page_idx}/{total_pages} "
                f"-> pdfplumber | chars={result.n_chars} "
                f"tables={result.n_tables} images={len(result.images)} "
                f"vlm_calls={result.vlm_calls}"
            )

            if result.markdown:
                md_parts.append(result.markdown)
                md_parts.append("")
            md_parts.append(page_marker(page_idx))
            md_parts.append("")

    stats.elapsed_sec = time.time() - t0
    if vlm is not None:
        stats.vlm_success = vlm.success_calls
        stats.vlm_failed = vlm.failed_calls

    logger.success(
        f"[PDF] 完成 {pdf_path.name} | {total_pages} 页 | "
        f"pdfplumber={stats.tier_pages[1]} | 表格={stats.total_tables} "
        f"图片={stats.total_images} "
        f"VLM调用={stats.vlm_attempted}(成功{stats.vlm_success}/失败{stats.vlm_failed}) "
        f"耗时={stats.elapsed_sec:.1f}s"
    )

    text = "\n".join(md_parts)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n", stats


def _extract_one_page(
    page: pdfplumber.page.Page,
    page_no: int,
    assets_dir: Path,
    doc_stem: str,
    vlm: MultiModalClient | None,
) -> PageResult:
    """提取单页：正文 + VLM 表格 + VLM 图片。"""
    table_infos = _find_tables(page, assets_dir, doc_stem, vlm)
    image_infos = _find_images(page)

    protected_bboxes = [info["bbox"] for info in table_infos + image_infos]
    text = _extract_text_excluding_bboxes(page, protected_bboxes)
    text_md = paragraphs_to_md(text.splitlines())

    parts: list[str] = []
    if text_md:
        parts.append(text_md)

    vlm_calls = 0
    image_paths: list[str] = []

    # 表格：截图 -> VLM 恢复 HTML table；失败时使用 pdfplumber 的结构化表格兜底。
    for idx, info in enumerate(table_infos, 1):
        img_path = _save_crop(page, info["bbox"], assets_dir, doc_stem, page_no, f"table{idx}")
        rel = f"./{assets_dir.name}/{img_path.name}"
        image_paths.append(rel)
        table_html = ""
        if vlm is not None:
            table_html = _normalize_table_html(
                vlm.describe_image(img_path, TABLE_PROMPT, max_tokens=3000)
            )
            vlm_calls += 1
        if not table_html and info.get("data"):
            table_html = pdfplumber_table_to_html(info["data"])
        if table_html:
            parts.append("")
            parts.append(f"**表格（第 {page_no} 页）**")
            parts.append("")
            parts.append(table_html)
        else:
            parts.append(f"![table]({rel})")

    # 图片：截图 -> VLM 描述。
    for idx, info in enumerate(image_infos, 1):
        img_path = _save_crop(page, info["bbox"], assets_dir, doc_stem, page_no, f"img{idx}")
        rel = f"./{assets_dir.name}/{img_path.name}"
        image_paths.append(rel)
        desc = ""
        if vlm is not None:
            desc = vlm.describe_image(img_path, IMAGE_PROMPT)
            vlm_calls += 1
        if desc:
            parts.append(f"![image]({rel}) <!-- desc: {desc} -->")
        else:
            parts.append(f"![image]({rel})")

    markdown = "\n\n".join(p for p in parts if p).strip()
    return PageResult(
        page_no=page_no,
        tier=1,
        markdown=markdown,
        images=image_paths,
        n_tables=len(table_infos),
        n_chars=len(text),
        vlm_calls=vlm_calls,
    )


_STRICT_SETTINGS = {
    "vertical_strategy": "lines",
    "horizontal_strategy": "lines",
    "snap_tolerance": 3,
    "join_tolerance": 3,
    "edge_min_length": 8,
    "intersection_tolerance": 5,
}

_LOOSE_SETTINGS = {
    "vertical_strategy": "lines_strict",
    "horizontal_strategy": "text",
    "snap_tolerance": 4,
    "join_tolerance": 4,
    "intersection_tolerance": 6,
}


def _find_tables(
    page: pdfplumber.page.Page,
    assets_dir: Path,
    doc_stem: str,
    vlm: MultiModalClient | None,
) -> list[dict]:
    """定位页面表格：lines 严格策略 + 评分过滤 + 可选 VLM 二次确认。

    流程：
    1. 先用 ``lines`` 严格策略（要求横竖线交叉），命中即视为高置信。
    2. 严格策略空手时，再用 ``lines_strict + text`` 宽松策略当候选。
    3. 对每个候选打分，明显不像表格的直接丢弃；
       低置信的候选若启用 ``TABLE_VLM_CLASSIFY``，再让多模态模型判一下"是不是表格"。
    """
    page_area = max(float(page.width) * float(page.height), 1.0)
    candidates: list[dict] = []

    def _collect(settings: dict, mode: str) -> list[dict]:
        out: list[dict] = []
        try:
            tables = page.find_tables(table_settings=settings)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"pdfplumber find_tables({mode}) 失败：{e}")
            return out
        for table in tables:
            bbox = _clamp_bbox(table.bbox, page)
            if not bbox:
                continue
            try:
                data = table.extract()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"table.extract() 失败({mode})：{e}")
                continue
            if not data:
                continue
            info = {"bbox": bbox, "data": data, "mode": mode}
            info.update(_score_table(info, page_area))
            out.append(info)
        return out

    # 1) 严格策略
    candidates = _collect(_STRICT_SETTINGS, "strict")

    # 2) 严格没找到才走宽松
    if not candidates:
        candidates = _collect(_LOOSE_SETTINGS, "loose")

    # 3) 评分 + 可选 VLM 二次确认
    accepted: list[dict] = []
    for idx, info in enumerate(candidates, 1):
        reason = info.get("reject_reason")
        if reason is None:
            logger.debug(
                f"[Table] 接受 mode={info['mode']} bbox={_fmt_bbox(info['bbox'])} "
                f"rows={info['rows']} cols={info['cols']} score={info['score']:.2f}"
            )
            accepted.append(info)
            continue

        # 低置信：strict 模式下相对可信，但若不满足规则，可让 VLM 二次确认；
        # loose 模式下置信度更低，仅在 VLM 开启时挽救。
        if vlm is not None and TABLE_VLM_CLASSIFY:
            tmp_path = _save_crop(
                page, info["bbox"], assets_dir, doc_stem,
                _page_no(page), f"cand{idx}"
            )
            verdict = vlm.is_table(tmp_path)
            if verdict is True:
                logger.info(
                    f"[Table] VLM 挽救：mode={info['mode']} reason={reason} -> 接受"
                )
                info["score"] = max(info["score"], 0.5)
                accepted.append(info)
            else:
                logger.debug(
                    f"[Table] 丢弃 mode={info['mode']} reason={reason} "
                    f"vlm={verdict} bbox={_fmt_bbox(info['bbox'])}"
                )
                _safe_unlink(tmp_path)
        else:
            logger.debug(
                f"[Table] 丢弃 mode={info['mode']} reason={reason} "
                f"bbox={_fmt_bbox(info['bbox'])}"
            )

    return accepted


def _score_table(info: dict, page_area: float) -> dict:
    """给表格候选打分；返回 {'score': float, 'rows': int, 'cols': int,
    'reject_reason': str|None}。"""
    data = info.get("data") or []
    bbox = info["bbox"]
    area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 0.0)
    area_ratio = area / page_area if page_area else 0.0

    rows = len(data)
    if rows == 0:
        return {"score": 0.0, "rows": 0, "cols": 0, "reject_reason": "no_rows"}

    col_counts = [len(r) for r in data]
    mode_cols = max(set(col_counts), key=col_counts.count)
    cols = mode_cols

    out: dict = {"rows": rows, "cols": cols, "score": 0.0, "reject_reason": None}

    # 1) 行列数下限
    if rows < TABLE_MIN_ROWS:
        out["reject_reason"] = f"rows<{TABLE_MIN_ROWS}"
        return out
    if cols < TABLE_MIN_COLS:
        out["reject_reason"] = f"cols<{TABLE_MIN_COLS}"
        return out

    # 2) 占页面积
    if area_ratio < TABLE_MIN_AREA_RATIO:
        out["reject_reason"] = f"area_ratio<{TABLE_MIN_AREA_RATIO}"
        return out
    if area_ratio > TABLE_MAX_AREA_RATIO:
        out["reject_reason"] = f"area_ratio>{TABLE_MAX_AREA_RATIO}"
        return out

    # 3) 列数一致性：偏离众数列数的行不能太多
    diff_rows = sum(1 for c in col_counts if c != mode_cols)
    var_ratio = diff_rows / rows
    if var_ratio > TABLE_MAX_COL_VAR_RATIO:
        out["reject_reason"] = f"col_var={var_ratio:.2f}>{TABLE_MAX_COL_VAR_RATIO}"
        return out

    # 4) cell 填充率与长度统计
    total_cells = 0
    filled_cells = 0
    too_long_cells = 0
    total_chars = 0
    for row in data:
        for cell in row:
            total_cells += 1
            text = (cell or "").strip()
            if text:
                filled_cells += 1
                total_chars += len(text)
                if len(text) > TABLE_MAX_CELL_CHARS:
                    too_long_cells += 1
    if total_cells == 0:
        out["reject_reason"] = "no_cells"
        return out

    filled_ratio = filled_cells / total_cells
    if filled_ratio < TABLE_MIN_FILLED_RATIO:
        out["reject_reason"] = f"filled_ratio={filled_ratio:.2f}<{TABLE_MIN_FILLED_RATIO}"
        return out

    avg_cell_chars = total_chars / max(filled_cells, 1)
    if avg_cell_chars > TABLE_MAX_AVG_CELL_CHARS:
        out["reject_reason"] = f"avg_cell_chars={avg_cell_chars:.0f}>{TABLE_MAX_AVG_CELL_CHARS}"
        return out

    # 超长 cell 占比过高：基本是多列正文段落
    if too_long_cells / total_cells > 0.2:
        out["reject_reason"] = f"too_long_cells={too_long_cells}/{total_cells}"
        return out

    # 5) 通过：给个综合分数（仅用于日志）
    score = (
        min(rows / 6, 1.0) * 0.3
        + min(cols / 4, 1.0) * 0.3
        + filled_ratio * 0.2
        + (1.0 - min(avg_cell_chars / TABLE_MAX_AVG_CELL_CHARS, 1.0)) * 0.2
    )
    out["score"] = float(score)
    return out


def _fmt_bbox(bbox: tuple[float, float, float, float]) -> str:
    return "({:.0f},{:.0f},{:.0f},{:.0f})".format(*bbox)


def _page_no(page: pdfplumber.page.Page) -> int:
    try:
        return int(page.page_number)
    except Exception:  # noqa: BLE001
        return 0


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _find_images(page: pdfplumber.page.Page) -> list[dict]:
    """定位页面图片；过滤极小装饰图。"""
    out: list[dict] = []
    page_area = max(float(page.width) * float(page.height), 1.0)
    for img in page.images:
        bbox = _clamp_bbox(
            (
                float(img["x0"]),
                float(img["top"]),
                float(img["x1"]),
                float(img["bottom"]),
            ),
            page,
        )
        if not bbox:
            continue
        area = max((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]), 0.0)
        if area / page_area < IMAGE_AREA_RATIO_FOR_VLM:
            continue
        out.append({"bbox": bbox})
    return _dedupe_bboxes(out)


def _extract_text_excluding_bboxes(
    page: pdfplumber.page.Page,
    bboxes: list[tuple[float, float, float, float]],
) -> str:
    """提取正文，排除表格/图片区域，减少重复。"""
    try:
        words = page.extract_words(
            x_tolerance=2,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=True,
        )
    except Exception:
        return page.extract_text(x_tolerance=2, y_tolerance=2) or ""

    kept = []
    for word in words:
        cx = (float(word["x0"]) + float(word["x1"])) / 2
        cy = (float(word["top"]) + float(word["bottom"])) / 2
        if any(_point_in_bbox(cx, cy, bbox) for bbox in bboxes):
            continue
        kept.append(word)

    if not kept:
        return ""

    lines: list[list[dict]] = []
    for word in sorted(kept, key=lambda w: (float(w["top"]), float(w["x0"]))):
        if not lines or abs(float(lines[-1][0]["top"]) - float(word["top"])) > 3:
            lines.append([word])
        else:
            lines[-1].append(word)

    out_lines: list[str] = []
    for line in lines:
        line = sorted(line, key=lambda w: float(w["x0"]))
        out_lines.append(" ".join(str(w["text"]) for w in line))
    return "\n".join(out_lines)


def _save_crop(
    page: pdfplumber.page.Page,
    bbox: tuple[float, float, float, float],
    assets_dir: Path,
    doc_stem: str,
    page_no: int,
    label: str,
) -> Path:
    """保存页面局部截图。"""
    assets_dir.mkdir(parents=True, exist_ok=True)
    out_path = assets_dir / f"{doc_stem}_p{page_no}_{label}.png"
    crop = page.crop(bbox)
    crop.to_image(resolution=200).save(str(out_path), format="PNG")
    return out_path


def _normalize_table_html(text: str) -> str:
    """从 VLM 输出中提取并清洗 HTML table。"""
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r"^```(?:html|markdown|md)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"<table[\s\S]*?</table>", text, flags=re.I)
    if not match:
        return ""
    table = match.group(0).strip()
    if "border=" not in table[:80]:
        table = re.sub(r"<table\b", '<table border="1" ', table, count=1, flags=re.I)
    return table


def _point_in_bbox(x: float, y: float, bbox: tuple[float, float, float, float]) -> bool:
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _clamp_bbox(
    bbox: tuple[float, float, float, float],
    page: pdfplumber.page.Page,
) -> tuple[float, float, float, float] | None:
    x0, top, x1, bottom = bbox
    x0 = max(0.0, min(float(x0), float(page.width)))
    x1 = max(0.0, min(float(x1), float(page.width)))
    top = max(0.0, min(float(top), float(page.height)))
    bottom = max(0.0, min(float(bottom), float(page.height)))
    if x1 <= x0 or bottom <= top:
        return None
    return (x0, top, x1, bottom)


def _dedupe_bboxes(items: list[dict]) -> list[dict]:
    """去掉几乎相同的图片框。"""
    result: list[dict] = []
    seen: set[tuple[int, int, int, int]] = set()
    for item in items:
        bbox = item["bbox"]
        key = tuple(round(v) for v in bbox)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
