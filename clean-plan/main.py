"""Markdown 清洗主入口。

用法：
    python clean-plan/main.py                          # raw-0/ -> clean/
    python clean-plan/main.py --file raw-0/某文件.md
    python clean-plan/main.py --rules-only             # 跳过 LLM
    python clean-plan/main.py --workers 4              # 文件级并发
    python clean-plan/main.py --overwrite              # 覆盖已存在
    python clean-plan/main.py --input-dir raw-0 --output-dir clean

结束后会在 ``clean/_clean_report.md`` 生成 markdown 汇总报告，
``clean/_clean.log`` 保留完整日志。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger  # noqa: E402

from cleaner import CleanLLM, CleanStats, clean_markdown_file  # noqa: E402
from config import DEFAULT_INPUT_DIR, DEFAULT_OUTPUT_DIR, DEFAULT_WORKERS  # noqa: E402


# ----------------- 文件收集 -----------------
def _collect_files(input_dir: Path, specific: Path | None) -> list[Path]:
    if specific is not None:
        return [specific] if specific.is_file() and specific.suffix.lower() == ".md" else []
    files: list[Path] = []
    for p in sorted(input_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".md":
            continue
        # 跳过我们自己生成的报告 / 日志
        if p.name.startswith("_"):
            continue
        files.append(p)
    return files


# ----------------- Markdown 报告 -----------------
def _md_escape(s: str) -> str:
    return s.replace("|", "\\|")


def _format_row(rec: dict) -> str:
    s: CleanStats | None = rec.get("stats")
    if s is None:
        return f"| {_md_escape(rec['file'])} | {rec['status']} | - | - | - | - | - | - | - |"
    rs = s.rule_stats
    rules_brief = (
        f"页码 {rs.page_numbers_removed} / 页眉页脚 {rs.boilerplate_removed} / 条 {rs.article_spaces_fixed} / 章 {rs.chapter_spaces_fixed}"
        f" / 节 {rs.section_spaces_fixed} / H1 {rs.heading_h1_fixed} H2 {rs.heading_h2_fixed}"
        f" 降正文 {rs.heading_demoted} / 加粗 {rs.bold_short_removed + rs.bold_article_removed}"
        f" / 中文空格 {rs.cn_inline_space_fixed} / 合并换行 {rs.linebreaks_merged}"
    )
    if s.chunks_skipped_rules_only:
        llm_brief = "rules-only"
    else:
        llm_brief = f"{s.chunks_success}/{s.chunks_total}（失败 {s.chunks_failed}）"
    return (
        f"| {_md_escape(rec['file'])} | {rec['status']} | {rules_brief} | {llm_brief} | "
        f"{s.tables_recovered} | "
        f"{s.chars_before} | {s.chars_after} | {s.chars_after - s.chars_before:+d} | "
        f"{s.elapsed_sec:.1f}s |"
    )


def write_markdown_report(
    records: list[dict], args: argparse.Namespace, output_dir: Path
) -> Path:
    path = output_dir / "_clean_report.md"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ok = [r for r in records if r["status"] == "ok"]
    failed = [r for r in records if r["status"] == "failed"]
    skipped = [r for r in records if r["status"].startswith("skipped")]

    # 累加统计
    chars_before_total = 0
    chars_after_total = 0
    chunks_total = 0
    chunks_ok = 0
    chunks_fail = 0
    tables_recovered_total = 0
    rule_acc = {
        "page": 0, "article": 0, "chapter": 0, "section": 0,
        "h1": 0, "h2": 0, "demoted": 0,
        "bold_short": 0, "bold_legal": 0, "cn_space": 0, "linebreaks": 0,
        "blanks": 0,
    }
    for r in ok:
        s: CleanStats = r["stats"]
        chars_before_total += s.chars_before
        chars_after_total += s.chars_after
        chunks_total += s.chunks_total
        chunks_ok += s.chunks_success
        chunks_fail += s.chunks_failed
        tables_recovered_total += s.tables_recovered
        rs = s.rule_stats
        rule_acc["page"] += rs.page_numbers_removed
        rule_acc["article"] += rs.article_spaces_fixed
        rule_acc["chapter"] += rs.chapter_spaces_fixed
        rule_acc["section"] += rs.section_spaces_fixed
        rule_acc["h1"] += rs.heading_h1_fixed
        rule_acc["h2"] += rs.heading_h2_fixed
        rule_acc["demoted"] += rs.heading_demoted
        rule_acc["bold_short"] += rs.bold_short_removed
        rule_acc["bold_legal"] += rs.bold_article_removed
        rule_acc["cn_space"] += rs.cn_inline_space_fixed
        rule_acc["linebreaks"] += rs.linebreaks_merged
        rule_acc["blanks"] += rs.blank_lines_compressed

    lines: list[str] = []
    lines.append("# Markdown 清洗运行报告")
    lines.append("")
    lines.append(f"- 运行时间：{now}")
    lines.append(f"- 输入目录：`{args.input_dir}`")
    lines.append(f"- 输出目录：`{args.output_dir}`")
    lines.append(
        f"- 参数：rules_only={args.rules_only}, workers={args.workers}, "
        f"overwrite={args.overwrite}"
    )
    lines.append("")

    lines.append("## 总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 文件总数 | {len(records)} |")
    lines.append(f"| ✓ 成功 | {len(ok)} |")
    lines.append(f"| ↷ 跳过 | {len(skipped)} |")
    lines.append(f"| ✗ 失败 | {len(failed)} |")
    lines.append(f"| 总字符（清洗前） | {chars_before_total} |")
    lines.append(f"| 总字符（清洗后） | {chars_after_total} |")
    lines.append(
        f"| LLM chunk 调用 | {chunks_ok}/{chunks_total}（失败 {chunks_fail}） |"
    )
    lines.append(f"| LLM 恢复表格数 | {tables_recovered_total} |")
    lines.append("")

    lines.append("## 本地规则命中（累计）")
    lines.append("")
    lines.append("| 规则 | 命中次数 |")
    lines.append("| --- | --- |")
    lines.append(f"| 删除页脚页码行 | {rule_acc['page']} |")
    lines.append(f"| 修复「第X条」空格 | {rule_acc['article']} |")
    lines.append(f"| 修复「第X章」空格 | {rule_acc['chapter']} |")
    lines.append(f"| 修复「第X节/项」空格 | {rule_acc['section']} |")
    lines.append(f"| 规范为一级标题（第X章） | {rule_acc['h1']} |")
    lines.append(f"| 规范为二级标题（第X节 / X.X） | {rule_acc['h2']} |")
    lines.append(f"| 降为正文（第X条 / X.X.X / 误标标题） | {rule_acc['demoted']} |")
    lines.append(f"| 去除短串错误加粗 | {rule_acc['bold_short']} |")
    lines.append(f"| 去除法条编号错误加粗 | {rule_acc['bold_legal']} |")
    lines.append(f"| 修复中文之间多余空格 | {rule_acc['cn_space']} |")
    lines.append(f"| 合并 PDF 硬换行 | {rule_acc['linebreaks']} |")
    lines.append(f"| 压缩多余空行 | {rule_acc['blanks']} |")
    lines.append("")

    lines.append("## 文件明细")
    lines.append("")
    lines.append(
        "| 文件 | 状态 | 本地规则命中 | LLM chunks | 恢复表格 | 清洗前字符 | 清洗后字符 | 变化 | 耗时 |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in records:
        lines.append(_format_row(r))
    lines.append("")

    if failed:
        lines.append("## 失败文件")
        lines.append("")
        lines.append("| 文件 | 错误 |")
        lines.append("| --- | --- |")
        for r in failed:
            lines.append(f"| {_md_escape(r['file'])} | {_md_escape(r.get('error', ''))} |")
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ----------------- 日志 -----------------
def _setup_logger(level: str, output_dir: Path) -> Path:
    log_path = output_dir / "_clean.log"
    logger.remove()
    fmt = "<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}"
    logger.add(sys.stderr, level=level, format=fmt, colorize=True)
    logger.add(
        log_path, level="DEBUG", rotation="10 MB", retention=5,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {message}",
    )
    return log_path


# ----------------- 单文件执行（供并发） -----------------
def _process_one(src: Path, dst: Path, llm: CleanLLM | None, rules_only: bool) -> dict:
    rec: dict = {
        "file": src.name,
        "status": "ok",
        "stats": None,
        "error": "",
    }
    try:
        stats = clean_markdown_file(src=src, dst=dst, llm=llm, rules_only=rules_only)
        rec["stats"] = stats
    except Exception as e:  # noqa: BLE001
        logger.exception(f"清洗失败：{src.name}")
        rec["status"] = "failed"
        rec["error"] = str(e)
    return rec


# ----------------- 入口 -----------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown 数据清洗流水线")
    parser.add_argument("--file", type=str, help="只处理指定 markdown 文件")
    parser.add_argument(
        "--input-dir", type=str, default=str(DEFAULT_INPUT_DIR),
        help="输入目录（默认 raw-0/）",
    )
    parser.add_argument(
        "--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR),
        help="输出目录（默认 clean/）",
    )
    parser.add_argument(
        "--rules-only", action="store_true",
        help="只跑本地规则，不调用 LLM",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help="并发文件数（默认 2）",
    )
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log_path = _setup_logger(args.log_level, output_dir)
    logger.info(f"日志文件：{log_path}")

    specific: Path | None = None
    if args.file:
        specific = Path(args.file).expanduser().resolve()
        if not specific.exists():
            logger.error(f"文件不存在：{specific}")
            sys.exit(1)

    files = _collect_files(input_dir, specific)
    if not files:
        logger.error(f"在 {input_dir} 没找到任何 .md 文件")
        sys.exit(1)

    llm: CleanLLM | None = None
    if not args.rules_only:
        llm = CleanLLM()
        logger.info(f"LLM 模型：{llm.model}")
    else:
        logger.info("当前为 rules-only 模式：不调用 LLM")

    logger.info("=" * 60)
    logger.info(f"开始清洗 | 文件 {len(files)} 个 | 输入 {input_dir} -> 输出 {output_dir}")
    logger.info(
        f"参数：rules_only={args.rules_only}, workers={args.workers}, "
        f"overwrite={args.overwrite}"
    )
    logger.info("=" * 60)

    records: list[dict] = []
    t_run = time.time()

    pending: list[tuple[Path, Path]] = []
    for f in files:
        out_path = output_dir / f.name
        if out_path.exists() and not args.overwrite:
            logger.info(f"跳过（已存在）：{out_path.name}  （加 --overwrite 可覆盖）")
            records.append(
                {"file": f.name, "status": "skipped_existing", "stats": None, "error": ""}
            )
            continue
        pending.append((f, out_path))

    if pending:
        if args.workers <= 1 or len(pending) <= 1:
            for src, dst in pending:
                logger.info("-" * 60)
                logger.info(f"处理：{src.name}")
                records.append(_process_one(src, dst, llm, args.rules_only))
        else:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(_process_one, src, dst, llm, args.rules_only): src
                    for src, dst in pending
                }
                for fut in as_completed(futures):
                    src = futures[fut]
                    try:
                        rec = fut.result()
                    except Exception as e:  # noqa: BLE001
                        logger.exception(f"线程异常：{src.name}")
                        rec = {"file": src.name, "status": "failed", "stats": None, "error": str(e)}
                    records.append(rec)

    logger.info("=" * 60)
    logger.success(f"清洗结束 | 总耗时 {time.time() - t_run:.1f}s")
    if llm is not None:
        logger.info(
            f"LLM 调用合计：{llm.total_calls} 次（成功 {llm.success_calls} / 失败 {llm.failed_calls}）"
        )

    report_path = write_markdown_report(records, args, output_dir)
    logger.success(f"清洗报告：{report_path}")


if __name__ == "__main__":
    main()
