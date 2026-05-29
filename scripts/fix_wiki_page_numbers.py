#!/usr/bin/env python3
"""仅修正 wiki 中 *-index.md 的页码，不重跑 split_wiki。

ocr-plan 在每页**末尾**插入 ``<!-- 第 N 页 -->``，标记之后属于第 N+1 页。
若曾用旧版 split_wiki（把标记当成「当前页」），条目录/摘要里的页码会整体偏早 1 页。

本脚本从 ``raw/`` 用修正后的页码逻辑重算条号→页码，再**就地替换** index 中的
「（第N页）」「（第N～M页）」，不改动正文 .md、不重置条文摘要内容。

用法（项目根目录）：

    python scripts/fix_wiki_page_numbers.py --dry-run
    python scripts/fix_wiki_page_numbers.py
    python scripts/fix_wiki_page_numbers.py --only 医疗器械使用质量监督管理办法
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from split_wiki import (  # noqa: E402
    ARTICLE_RE,
    GUIDE_CLAUSE_LINE_RE,
    SUBSECTION_NO_RE,
    extract_outline_entries,
    first_content_index,
    line_body_after_page_marker,
)

_REPO = _SCRIPTS_DIR.parent
_RAW = _REPO / "raw"
_WIKI = _REPO / "wiki"

CATALOG_PAGE_RE = re.compile(r"^(\s{2}- )(.+?)（第(\d+)页）\s*$")
SUMMARY_PAGE_RE = re.compile(r"^(- \*\*)(.+?)（第(\d+)页）：")
FILE_PAGE_RE = re.compile(r"^(- )(.+?)（第(\d+)～(\d+)页）(：.*)?\s*$")
SUMMARY_FILE_PAGE_RE = re.compile(r"^(- \*\*)(.+?)（第(\d+)～(\d+)页）：")


def _repo_root() -> Path:
    return _REPO


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def build_page_map(raw_path: Path) -> dict[str, int]:
    lines = _read_lines(raw_path)
    start = first_content_index(lines, raw_path.name)
    if start is None:
        return {}
    entries = extract_outline_entries(lines[start:], start_page=1)
    return {e.label: e.page for e in entries}


def _label_from_body_line(line: str) -> str | None:
    body = line_body_after_page_marker(line)
    if not body:
        return None
    am = ARTICLE_RE.match(body)
    if am:
        return am.group(0).strip().rstrip(":：")
    sm = SUBSECTION_NO_RE.match(body)
    if sm:
        return sm.group(1)
    gm = GUIDE_CLAUSE_LINE_RE.match(body)
    if gm:
        return gm.group(1)
    return None


def page_range_for_body(body_path: Path, page_map: dict[str, int]) -> tuple[int, int] | None:
    pages: list[int] = []
    for line in _read_lines(body_path):
        label = _label_from_body_line(line)
        if label and label in page_map:
            pages.append(page_map[label])
    if not pages:
        return None
    return min(pages), max(pages)


def _format_page_range(sp: int, ep: int) -> str:
    if sp == ep:
        return f"第{sp}页"
    return f"第{sp}～{ep}页"


def patch_index_file(
    index_path: Path,
    page_map: dict[str, int],
    body_page_ranges: dict[str, tuple[int, int]],
    *,
    dry_run: bool,
) -> list[str]:
    """返回变更说明列表。"""
    changes: list[str] = []
    out_lines: list[str] = []
    stem = index_path.name[: -len("-index.md")] if index_path.name.endswith("-index.md") else ""

    for line in _read_lines(index_path):
        new_line = line

        m = CATALOG_PAGE_RE.match(line)
        if m:
            label = m.group(2).strip()
            if label in page_map:
                new_page = page_map[label]
                old_page = int(m.group(3))
                if new_page != old_page:
                    changes.append(f"{index_path.name}: 条目录 {label} {old_page}→{new_page}")
                new_line = f"{m.group(1)}{label}（第{new_page}页）"

        m = SUMMARY_PAGE_RE.match(line)
        if m:
            label = m.group(2).strip()
            if label in page_map:
                new_page = page_map[label]
                old_page = int(m.group(3))
                if new_page != old_page:
                    changes.append(f"{index_path.name}: 摘要 {label} {old_page}→{new_page}")
                new_line = f"{m.group(1)}{label}（第{new_page}页）："

        m = FILE_PAGE_RE.match(line)
        if m:
            name = m.group(2).strip()
            if name in body_page_ranges:
                sp, ep = body_page_ranges[name]
                old_sp, old_ep = int(m.group(3)), int(m.group(4))
                tail = m.group(5) or ""
                if (sp, ep) != (old_sp, old_ep):
                    changes.append(
                        f"{index_path.name}: 文件目录 {name} "
                        f"{old_sp}～{old_ep}→{sp}～{ep}"
                    )
                new_line = f"{m.group(1)}{name}（第{sp}～{ep}页）{tail}"

        m = SUMMARY_FILE_PAGE_RE.match(line)
        if m:
            name = m.group(2).strip()
            if name in body_page_ranges:
                sp, ep = body_page_ranges[name]
                old_sp, old_ep = int(m.group(3)), int(m.group(4))
                if (sp, ep) != (old_sp, old_ep):
                    changes.append(
                        f"{index_path.name}: 各文件摘要 {name} "
                        f"{old_sp}～{old_ep}→{sp}～{ep}"
                    )
                new_line = f"{m.group(1)}{name}（第{sp}～{ep}页）："

        out_lines.append(new_line)

    if changes and not dry_run:
        index_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return changes


def fix_norm(
    norm_name: str,
    *,
    raw_dir: Path,
    wiki_root: Path,
    dry_run: bool,
) -> list[str]:
    raw_path = raw_dir / f"{norm_name}.md"
    wiki_dir = wiki_root / norm_name
    if not raw_path.is_file():
        raise FileNotFoundError(f"缺少 raw：{raw_path}")
    if not wiki_dir.is_dir():
        raise FileNotFoundError(f"缺少 wiki：{wiki_dir}")

    page_map = build_page_map(raw_path)
    if not page_map:
        return [f"{norm_name}: raw 未解析到条号，跳过"]

    body_page_ranges: dict[str, tuple[int, int]] = {}
    for body in sorted(wiki_dir.rglob("*.md")):
        if body.name.endswith("-index.md"):
            continue
        if body.name == f"{norm_name}-index.md":
            continue
        stem = body.stem
        pr = page_range_for_body(body, page_map)
        if pr:
            body_page_ranges[stem] = pr

    all_changes: list[str] = []
    for index_path in sorted(wiki_dir.rglob("*-index.md")):
        all_changes.extend(
            patch_index_file(
                index_path, page_map, body_page_ranges, dry_run=dry_run
            )
        )
    return all_changes


def main() -> int:
    parser = argparse.ArgumentParser(description="修正 wiki index 页码（不重拆正文）")
    parser.add_argument(
        "--wiki-root",
        type=Path,
        default=_WIKI,
        help="wiki 根目录，默认 wiki/",
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=_RAW,
        help="raw 目录，默认 raw/",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="只处理指定规范名（可多次，不含 .md）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将修改的项，不写文件",
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

    total = 0
    for norm in norms:
        try:
            changes = fix_norm(
                norm, raw_dir=raw_dir, wiki_root=wiki_root, dry_run=args.dry_run
            )
        except FileNotFoundError as e:
            print(f"跳过 {norm}: {e}")
            continue
        if not changes:
            print(f"{norm}: 无需修改")
            continue
        print(f"\n=== {norm} ({len(changes)} 处) ===")
        for c in changes[:40]:
            print(f"  {c}")
        if len(changes) > 40:
            print(f"  … 另有 {len(changes) - 40} 处")
        total += len(changes)

    mode = "（dry-run，未写入）" if args.dry_run else "已写入"
    print(f"\n合计 {total} 处页码修正 {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
