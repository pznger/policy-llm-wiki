#!/usr/bin/env python3
"""粗略比较"源 markdown 行数"与"拆分后非 -index.md 文件总行数"。

经验上，因为页码标记、空行压缩、章节前后无关内容被剔除等原因，拆分后正文
行数比源文件少 5% ～ 20% 通常是正常的。差距过大或反向暴增都需要检查：
1. 是否误删正文条款；
2. 是否错误跳过了某个章；
3. 是否因分段重叠 / 标题补回导致重复过多。
"""
from __future__ import annotations

import argparse
from pathlib import Path


def count_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def find_output_root(source: Path, output_root: Path) -> Path:
    spec_root = output_root / source.stem
    if not spec_root.exists():
        raise FileNotFoundError(f"未找到拆分输出目录：{spec_root}")
    return spec_root


def iter_non_index_files(spec_root: Path) -> list[Path]:
    return sorted(
        p for p in spec_root.rglob("*.md")
        if not p.name.endswith("-index.md")
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="对比源 markdown 与拆分后非 index 文件的总行数"
    )
    parser.add_argument("source", help="源 markdown 文件，例如 raw/药物警戒质量管理规范.md")
    parser.add_argument("--output-root", default="wiki", help="wiki 根目录（默认 wiki）")
    parser.add_argument("--details", action="store_true", help="打印每个文件的行数")
    args = parser.parse_args()

    source = Path(args.source).resolve()
    output_root = Path(args.output_root).resolve()
    spec_root = find_output_root(source, output_root)

    src_lines = count_lines(source)
    body_files = iter_non_index_files(spec_root)
    body_total = sum(count_lines(p) for p in body_files)
    diff = body_total - src_lines
    ratio = (body_total / src_lines) if src_lines else 0.0

    print(f"源文件：{source}")
    print(f"拆分目录：{spec_root}")
    print(f"源文件总行数：{src_lines}")
    print(f"拆分后非 index 文件数：{len(body_files)}")
    print(f"拆分后非 index 文件总行数：{body_total}")
    print(f"行数差值（拆分后-源文件）：{diff:+d}")
    print(f"行数比值（拆分后/源文件）：{ratio:.4f}")

    if args.details:
        print()
        print("各文件行数：")
        for path in body_files:
            print(f"  {path.relative_to(spec_root)}\t{count_lines(path)}")


if __name__ == "__main__":
    main()
