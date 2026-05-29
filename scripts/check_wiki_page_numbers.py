#!/usr/bin/env python3
"""检查 wiki *-index.md 中的页码是否与 raw/ 重算结果一致。

依据与 ``fix_wiki_page_numbers.py`` 相同：``<!-- 第 N 页 -->`` 表示第 N 页结束，其后为第 N+1 页。

用法（项目根目录）：

    python scripts/check_wiki_page_numbers.py
    python scripts/check_wiki_page_numbers.py --only 医疗器械监督管理条例
    python scripts/check_wiki_page_numbers.py --report qa-test/_page_check.md
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fix_wiki_page_numbers import (  # noqa: E402
    CATALOG_PAGE_RE,
    FILE_PAGE_RE,
    SUMMARY_FILE_PAGE_RE,
    SUMMARY_PAGE_RE,
    _read_lines,
    build_page_map,
    fix_norm,
    page_range_for_body,
)

_REPO = _SCRIPTS_DIR.parent
_RAW = _REPO / "raw"
_WIKI = _REPO / "wiki"


def _collect_index_page_refs(index_path: Path) -> list[dict]:
    """从 index 收集所有带页码的行。"""
    refs: list[dict] = []
    for line in _read_lines(index_path):
        m = CATALOG_PAGE_RE.match(line)
        if m:
            refs.append(
                {
                    "kind": "条目录",
                    "label": m.group(2).strip(),
                    "old": int(m.group(3)),
                    "line": line,
                }
            )
            continue
        m = SUMMARY_PAGE_RE.match(line)
        if m:
            refs.append(
                {
                    "kind": "条文摘要",
                    "label": m.group(2).strip(),
                    "old": int(m.group(3)),
                    "line": line,
                }
            )
            continue
        m = FILE_PAGE_RE.match(line)
        if m:
            refs.append(
                {
                    "kind": "文件目录",
                    "label": m.group(2).strip(),
                    "old": (int(m.group(3)), int(m.group(4))),
                    "line": line,
                }
            )
            continue
        m = SUMMARY_FILE_PAGE_RE.match(line)
        if m:
            refs.append(
                {
                    "kind": "各文件摘要",
                    "label": m.group(2).strip(),
                    "old": (int(m.group(3)), int(m.group(4))),
                    "line": line,
                }
            )
    return refs


def check_norm(
    norm: str,
    *,
    raw_dir: Path,
    wiki_root: Path,
) -> tuple[list[dict], list[str]]:
    """返回 (不一致列表, 警告列表)。"""
    raw_path = raw_dir / f"{norm}.md"
    wiki_dir = wiki_root / norm
    issues: list[dict] = []
    warnings: list[str] = []

    if not raw_path.is_file():
        return issues, [f"缺少 raw/{norm}.md"]
    if not wiki_dir.is_dir():
        return issues, [f"缺少 wiki/{norm}/"]

    page_map = build_page_map(raw_path)
    if not page_map:
        warnings.append(f"{norm}: raw 未解析到条号/小节（指南类或结构特殊）")

    body_page_ranges: dict[str, tuple[int, int]] = {}
    for body in sorted(wiki_dir.rglob("*.md")):
        if body.name.endswith("-index.md"):
            continue
        if body.name == f"{norm}-index.md":
            continue
        pr = page_range_for_body(body, page_map) if page_map else None
        if pr:
            body_page_ranges[body.stem] = pr

    for index_path in sorted(wiki_dir.rglob("*-index.md")):
        for ref in _collect_index_page_refs(index_path):
            label = ref["label"]
            kind = ref["kind"]
            rel = index_path.relative_to(wiki_dir).as_posix()

            if kind in ("条目录", "条文摘要"):
                if not page_map:
                    continue
                expected = page_map.get(label)
                if expected is None:
                    continue
                old = ref["old"]
                if old != expected:
                    issues.append(
                        {
                            "norm": norm,
                            "index": rel,
                            "kind": kind,
                            "label": label,
                            "old": old,
                            "expected": expected,
                            "delta": expected - old,
                        }
                    )
            else:
                if label not in body_page_ranges:
                    continue
                sp, ep = body_page_ranges[label]
                old_sp, old_ep = ref["old"]
                if (old_sp, old_ep) != (sp, ep):
                    issues.append(
                        {
                            "norm": norm,
                            "index": rel,
                            "kind": kind,
                            "label": label,
                            "old": f"{old_sp}～{old_ep}",
                            "expected": f"{sp}～{ep}",
                            "delta": None,
                        }
                    )

    return issues, warnings


def write_report(path: Path, all_issues: list[dict], warnings: list[str]) -> None:
    by_norm: dict[str, list[dict]] = defaultdict(list)
    for it in all_issues:
        by_norm[it["norm"]].append(it)

    lines = [
        "# Wiki 页码检查报告",
        "",
        f"共 **{len(all_issues)}** 处 index 页码与 raw 重算不一致。",
        "",
        "修复：`python scripts/fix_wiki_page_numbers.py`",
        "",
    ]
    if warnings:
        lines.append("## 警告\n")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    for norm in sorted(by_norm):
        items = by_norm[norm]
        deltas = [it["delta"] for it in items if it.get("delta") is not None]
        d_summary = ""
        if deltas:
            uniq = sorted(set(deltas))
            d_summary = f"（条号 delta: {uniq}）"
        lines.append(f"## {norm}（{len(items)} 处）{d_summary}\n")
        lines.append("| 类型 | 标签 | index | 当前 | 应为 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for it in items[:200]:
            lines.append(
                f"| {it['kind']} | {it['label']} | `{it['index']}` | "
                f"{it['old']} | {it['expected']} |"
            )
        if len(items) > 200:
            lines.append(f"\n… 另有 {len(items) - 200} 处")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 wiki index 页码")
    parser.add_argument("--wiki-root", type=Path, default=_WIKI)
    parser.add_argument("--raw-dir", type=Path, default=_RAW)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="写出 Markdown 报告路径",
    )
    args = parser.parse_args()

    raw_dir = args.raw_dir.resolve()
    wiki_root = args.wiki_root.resolve()

    if args.only:
        norms = [n.removesuffix(".md") for n in args.only]
    else:
        norms = sorted(
            p.stem for p in raw_dir.glob("*.md") if (wiki_root / p.stem).is_dir()
        )

    all_issues: list[dict] = []
    all_warnings: list[str] = []

    for norm in norms:
        issues, warnings = check_norm(norm, raw_dir=raw_dir, wiki_root=wiki_root)
        all_issues.extend(issues)
        all_warnings.extend(warnings)
        if issues:
            print(f"{norm}: {len(issues)} 处不一致")
        else:
            print(f"{norm}: OK")

    print(f"\n合计 {len(all_issues)} 处需修正")

    if args.report:
        write_report(args.report.resolve(), all_issues, all_warnings)
        print(f"报告已写入 {args.report}")

    return 1 if all_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
