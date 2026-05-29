#!/usr/bin/env python3
"""批量把 raw/*.md 拆分到 wiki/<规范名>/。

用法（在项目根目录执行）：

    python scripts/batch_split_wiki.py
    python scripts/batch_split_wiki.py --raw-dir raw --output-root wiki
    python scripts/batch_split_wiki.py --dry-run
    python scripts/batch_split_wiki.py --only 药物警戒质量管理规范.md

拆分后可用 count_split_lines.py 抽查，或加 --report 生成本次汇总。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许从项目根或 scripts/ 目录运行
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from split_wiki import SplitWikiResult, split_wiki_file  # noqa: E402


def _repo_root() -> Path:
    return _SCRIPTS_DIR.parent


def collect_sources(raw_dir: Path, only: list[str] | None) -> list[Path]:
    if only:
        files: list[Path] = []
        for name in only:
            p = raw_dir / name
            if not p.suffix:
                p = p.with_suffix(".md")
            if not p.is_file():
                raise FileNotFoundError(f"未找到：{p}")
            files.append(p.resolve())
        return files

    return sorted(
        p.resolve()
        for p in raw_dir.glob("*.md")
        if p.is_file() and not p.name.startswith("_")
    )


def _line_ratio(result: SplitWikiResult) -> float:
    if result.src_line_count == 0:
        return 0.0
    return result.body_line_count / result.src_line_count


def _format_row(result: SplitWikiResult) -> str:
    ratio = _line_ratio(result)
    diff = result.body_line_count - result.src_line_count
    flag = ""
    if ratio > 1.05 or ratio < 0.75:
        flag = " [!]"
    return (
        f"| {result.source.name} | {result.chapter_count} | {result.body_file_count} "
        f"| {result.src_line_count} | {result.body_line_count} | {diff:+d} | {ratio:.2%}{flag} |"
    )


def write_report(results: list[SplitWikiResult], report_path: Path) -> None:
    lines = [
        "# Wiki 批量拆分报告",
        "",
        f"- 成功：{len(results)} 份",
        "",
        "| 源文件 | 章数 | 正文文件数 | 源行数 | 拆分行数 | 差值 | 比值 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(_format_row(r) for r in results)
    lines.extend(
        [
            "",
            "说明：比值通常在 80%～95%（拆分后略少于源文件）；标 `[!]` 建议用 "
            "`python scripts/count_split_lines.py raw/<文件>.md --details` 复查。",
            "",
        ]
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="批量拆分 raw/*.md 到 wiki/")
    parser.add_argument(
        "--raw-dir",
        default="raw",
        help="源 markdown 目录（默认 raw）",
    )
    parser.add_argument(
        "--output-root",
        default="wiki",
        help="wiki 输出根目录（默认 wiki）",
    )
    parser.add_argument(
        "--max-pages-per-section",
        type=int,
        default=2,
        help="单章/节超过多少页继续切分（默认 2）",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="FILE",
        help="只处理指定文件名，如 药物警戒质量管理规范.md",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出将要处理的文件，不实际拆分",
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const="wiki/_batch_split_report.md",
        metavar="PATH",
        help="写入汇总报告（默认 wiki/_batch_split_report.md）",
    )
    args = parser.parse_args()

    root = _repo_root()
    raw_dir = (root / args.raw_dir).resolve()
    output_root = (root / args.output_root).resolve()

    if not raw_dir.is_dir():
        print(f"错误：raw 目录不存在：{raw_dir}", file=sys.stderr)
        return 1

    sources = collect_sources(raw_dir, args.only)
    if not sources:
        print(f"错误：{raw_dir} 下没有可拆分的 .md 文件", file=sys.stderr)
        return 1

    print(f"raw 目录：{raw_dir}")
    print(f"输出根目录：{output_root}")
    print(f"待处理：{len(sources)} 份")
    print("-" * 60)

    if args.dry_run:
        for p in sources:
            print(p.name)
        return 0

    results: list[SplitWikiResult] = []
    failed = 0

    for i, source in enumerate(sources, 1):
        print(f"[{i}/{len(sources)}] {source.name} ...", flush=True)
        try:
            result = split_wiki_file(
                source,
                output_root=output_root,
                max_pages_per_section=args.max_pages_per_section,
            )
            results.append(result)
            ratio = _line_ratio(result)
            print(
                f"  -> {result.output_root.name}/  "
                f"章 {result.chapter_count}  正文 {result.body_file_count}  "
                f"行 {result.body_line_count}/{result.src_line_count} ({ratio:.1%})"
            )
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  !! 失败：{e}", file=sys.stderr)

    print("-" * 60)
    print(f"完成：成功 {len(results)}，失败 {failed}")

    if args.report and results:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = root / report_path
        write_report(results, report_path)
        print(f"报告：{report_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
