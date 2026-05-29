"""文档提取主入口。

用法：
    python ocr-plan/main.py                       # 处理 data/ 下全部文档
    python ocr-plan/main.py --file data/某文件.pdf
    python ocr-plan/main.py --no-vlm              # 不调用多模态 LLM（表格会使用 pdfplumber 兜底）
    python ocr-plan/main.py --no-vlm              # 不调用多模态 LLM
    python ocr-plan/main.py --overwrite           # 覆盖已存在的 .md
    python ocr-plan/main.py --log-level DEBUG     # 控制台日志等级

每次运行结束会在 raw/_extract_report.md 生成 markdown 汇总报告，
同时在 raw/_extract.log 保留完整文本日志。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger  # noqa: E402

from config import DATA_DIR, RAW_DIR  # noqa: E402
from extractors.docx_extractor import DocxStats, extract_docx  # noqa: E402
from extractors.pdf_extractor import PdfStats, extract_pdf  # noqa: E402

SUPPORTED_EXT = {".pdf", ".docx", ".doc"}


# ----------------- 文件收集 -----------------
def _collect_files(data_dir: Path, specific: Path | None) -> list[Path]:
    if specific is not None:
        return [specific] if specific.suffix.lower() in SUPPORTED_EXT else []
    files: list[Path] = []
    for p in sorted(data_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
            files.append(p)
    return files


def _dedup_by_stem(files: list[Path]) -> list[Path]:
    """同一文档可能同时有 .doc 与 .docx；优先 .docx > .pdf > .doc。"""
    priority = {".docx": 0, ".pdf": 1, ".doc": 2}
    by_stem: dict[str, Path] = {}
    for f in files:
        stem = f.stem
        if stem not in by_stem or priority[f.suffix.lower()] < priority[by_stem[stem].suffix.lower()]:
            by_stem[stem] = f
    return sorted(by_stem.values())


# ----------------- 处理单个文件 -----------------
def process_one(file_path: Path, args: argparse.Namespace) -> dict:
    """返回该文件的处理记录（用于报告）。"""
    record: dict = {
        "file": file_path.name,
        "ext": file_path.suffix.lower(),
        "kind": "",
        "out": "",
        "status": "skipped",
        "error": "",
        "elapsed_sec": 0.0,
        "n_chars": 0,
        "pdf_stats": None,
        "docx_stats": None,
    }

    out_path = RAW_DIR / f"{file_path.stem}.md"
    record["out"] = out_path.name

    if out_path.exists() and not args.overwrite:
        logger.info(f"跳过（已存在）：{out_path.name}  （加 --overwrite 可覆盖）")
        record["status"] = "skipped_existing"
        return record

    ext = file_path.suffix.lower()
    t0 = time.time()
    try:
        if ext == ".pdf":
            record["kind"] = "pdf"
            md, pdf_stats = extract_pdf(
                file_path,
                raw_dir=RAW_DIR,
                use_vlm=not args.no_vlm,
                force_tier=args.force_tier,
            )
            record["pdf_stats"] = pdf_stats
        elif ext in {".docx", ".doc"}:
            record["kind"] = "doc/docx"
            md, docx_stats = extract_docx(file_path)
            record["docx_stats"] = docx_stats
        else:
            logger.warning(f"忽略不支持的文件：{file_path}")
            record["status"] = "unsupported"
            return record
    except Exception as e:  # noqa: BLE001
        logger.exception(f"处理失败：{file_path.name}: {e}")
        record["status"] = "failed"
        record["error"] = str(e)
        record["elapsed_sec"] = time.time() - t0
        return record

    out_path.write_text(md, encoding="utf-8")
    record["status"] = "ok"
    record["n_chars"] = len(md)
    record["elapsed_sec"] = time.time() - t0
    logger.success(
        f"输出：{file_path.name} -> {out_path.name} "
        f"({len(md)} chars, {record['elapsed_sec']:.1f}s)"
    )
    return record


# ----------------- Markdown 报告 -----------------
def _md_escape(s: str) -> str:
    return s.replace("|", "\\|")


def _format_pdf_row(rec: dict) -> str:
    s: PdfStats | None = rec.get("pdf_stats")
    if s is None:
        return ""
    parser_str = f"pdfplumber={s.tier_pages[1]}"
    vlm_str = (
        f"{s.vlm_attempted}(ok {s.vlm_success}/fail {s.vlm_failed})"
        if s.vlm_enabled else "off"
    )
    return (
        f"| {_md_escape(rec['file'])} | PDF | {rec['status']} | "
        f"{s.total_pages} | {parser_str} | {s.total_tables} | {s.total_images} | "
        f"{vlm_str} | {s.elapsed_sec:.1f}s | {rec['n_chars']} |"
    )


def _format_docx_row(rec: dict) -> str:
    s: DocxStats | None = rec.get("docx_stats")
    if s is None:
        return ""
    extra = " (from .doc)" if s.converted_from_doc else ""
    return (
        f"| {_md_escape(rec['file'])} | DOCX{extra} | {rec['status']} | "
        f"段落 {s.n_paragraphs} / 标题 {s.n_headings} / 表格 {s.n_tables} | "
        f"{s.elapsed_sec:.1f}s | {s.n_chars} |"
    )


def write_markdown_report(records: list[dict], args: argparse.Namespace) -> Path:
    """把整次运行的汇总写到 raw/_extract_report.md。"""
    report_path = RAW_DIR / "_extract_report.md"

    pdf_recs = [r for r in records if r["kind"] == "pdf"]
    doc_recs = [r for r in records if r["kind"] == "doc/docx"]
    failed = [r for r in records if r["status"] == "failed"]
    ok = [r for r in records if r["status"] == "ok"]
    skipped = [r for r in records if r["status"].startswith("skipped")]

    total_pages = sum((r["pdf_stats"].total_pages for r in pdf_recs if r["pdf_stats"]), 0)
    pdfplumber_pages = 0
    total_tables = 0
    total_images = 0
    vlm_total = 0
    vlm_ok = 0
    vlm_fail = 0
    for r in pdf_recs:
        s = r["pdf_stats"]
        if not s:
            continue
        pdfplumber_pages += s.tier_pages[1]
        total_tables += s.total_tables
        total_images += s.total_images
        vlm_total += s.vlm_attempted
        vlm_ok += s.vlm_success
        vlm_fail += s.vlm_failed

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append("# 文档提取运行报告")
    lines.append("")
    lines.append(f"- 运行时间：{now}")
    lines.append(f"- 数据目录：`{DATA_DIR}`")
    lines.append(f"- 输出目录：`{RAW_DIR}`")
    lines.append(
        f"- 参数：force_tier={args.force_tier}, no_vlm={args.no_vlm}, overwrite={args.overwrite}"
    )
    lines.append("")

    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 处理文件总数 | {len(records)} |")
    lines.append(f"| ✓ 成功 | {len(ok)} |")
    lines.append(f"| ↷ 跳过 | {len(skipped)} |")
    lines.append(f"| ✗ 失败 | {len(failed)} |")
    lines.append(f"| PDF 总页数 | {total_pages} |")
    lines.append(f"| PDF 解析页数 | pdfplumber {pdfplumber_pages} |")
    lines.append(f"| 提取表格总数 | {total_tables} |")
    lines.append(f"| 抽取图片总数 | {total_images} |")
    lines.append(
        f"| 多模态调用 | {vlm_total} 次（成功 {vlm_ok} / 失败 {vlm_fail}）"
    )
    lines.append("")

    if pdf_recs:
        lines.append("## PDF 文件明细")
        lines.append("")
        lines.append(
            "| 文件 | 类型 | 状态 | 页数 | PDF解析 | 表格 | 图片 | 多模态调用 | 耗时 | 输出字符 |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for r in pdf_recs:
            row = _format_pdf_row(r)
            if row:
                lines.append(row)
            else:
                lines.append(
                    f"| {_md_escape(r['file'])} | PDF | {r['status']} | - | - | - | - | - | "
                    f"{r['elapsed_sec']:.1f}s | {r['n_chars']} |"
                )
        lines.append("")

    if doc_recs:
        lines.append("## DOC / DOCX 文件明细")
        lines.append("")
        lines.append("| 文件 | 类型 | 状态 | 结构统计 | 耗时 | 输出字符 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for r in doc_recs:
            row = _format_docx_row(r)
            if row:
                lines.append(row)
            else:
                lines.append(
                    f"| {_md_escape(r['file'])} | DOCX | {r['status']} | - | "
                    f"{r['elapsed_sec']:.1f}s | {r['n_chars']} |"
                )
        lines.append("")

    # 回退原因明细（哪些页发生了回退）
    fb_blocks: list[str] = []
    for r in pdf_recs:
        s = r["pdf_stats"]
        if s and s.fallback_reasons:
            fb_blocks.append(f"### {r['file']}")
            fb_blocks.append("")
            for line in s.fallback_reasons:
                fb_blocks.append(f"- {line}")
            fb_blocks.append("")
    if fb_blocks:
        lines.append("## 回退原因（按文档）")
        lines.append("")
        lines.extend(fb_blocks)

    if failed:
        lines.append("## 失败文件")
        lines.append("")
        lines.append("| 文件 | 错误 |")
        lines.append("| --- | --- |")
        for r in failed:
            lines.append(f"| {_md_escape(r['file'])} | {_md_escape(r['error'])} |")
        lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


# ----------------- 日志配置 -----------------
def _setup_logger(level: str) -> Path:
    log_path = RAW_DIR / "_extract.log"
    logger.remove()
    fmt = "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}"
    logger.add(sys.stderr, level=level, format=fmt, colorize=True)
    logger.add(
        log_path,
        level="DEBUG",
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
    )
    return log_path


# ----------------- 入口 -----------------
def main() -> None:
    parser = argparse.ArgumentParser(description="政策文档 -> markdown 提取流水线")
    parser.add_argument("--file", type=str, help="只处理指定文件（支持相对/绝对路径）")
    parser.add_argument(
        "--force-tier", type=int, choices=[1, 2, 3], default=None,
        help="兼容旧参数；当前 PDF 仅使用 pdfplumber，传入后会被忽略",
    )
    parser.add_argument("--no-vlm", action="store_true", help="不调用多模态 LLM 描述图片")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的 .md")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"],
        help="控制台日志等级（DEBUG 会显示每页的更多细节）",
    )
    args = parser.parse_args()

    log_path = _setup_logger(args.log_level)
    logger.info(f"日志文件：{log_path}")

    specific: Path | None = None
    if args.file:
        specific = Path(args.file).expanduser().resolve()
        if not specific.exists():
            logger.error(f"文件不存在：{specific}")
            sys.exit(1)

    files = _collect_files(DATA_DIR, specific)
    if specific is None:
        files = _dedup_by_stem(files)

    if not files:
        logger.error(f"在 {DATA_DIR} 没找到任何 PDF/DOCX/DOC 文件")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info(f"开始运行 | 共 {len(files)} 个文件 | 输出目录：{RAW_DIR}")
    logger.info(
        f"参数: force_tier={args.force_tier}, no_vlm={args.no_vlm}, "
        f"overwrite={args.overwrite}, log_level={args.log_level}"
    )
    logger.info("=" * 60)

    records: list[dict] = []
    t_run = time.time()
    for idx, f in enumerate(files, 1):
        logger.info("-" * 60)
        logger.info(f"[{idx}/{len(files)}] 处理：{f.name}  ({f.suffix.lower()})")
        rec = process_one(f, args)
        records.append(rec)

    logger.info("=" * 60)
    logger.success(f"全部完成 | 总耗时 {time.time() - t_run:.1f}s")

    report_path = write_markdown_report(records, args)
    logger.success(f"提取报告：{report_path}")


if __name__ == "__main__":
    main()
