"""文本质量评估：判断单页提取结果是否需要回退到下一层。"""
from __future__ import annotations

import re

from config import MAX_GARBLED_RATIO, MIN_CHARS_PER_PAGE, MIN_CN_RATIO

_CN_PATTERN = re.compile(r"[\u4e00-\u9fff]")
_PRINTABLE_PATTERN = re.compile(
    r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefA-Za-z0-9\s,.，。；：、:;！？!?\-\+\*/=（）()\[\]【】《》\"'·…—\\]"
)


def text_quality_ok(text: str) -> tuple[bool, str]:
    """返回 (是否合格, 原因)。"""
    if not text:
        return False, "empty"

    stripped = "".join(text.split())
    if len(stripped) < MIN_CHARS_PER_PAGE:
        return False, f"too_short({len(stripped)} < {MIN_CHARS_PER_PAGE})"

    cn_count = len(_CN_PATTERN.findall(stripped))
    cn_ratio = cn_count / max(len(stripped), 1)
    # 仅当文本主体看起来是中文文档但占比过低时才判失败；纯英文文档应放行
    if cn_count > 0 and cn_ratio < MIN_CN_RATIO and len(stripped) > 80:
        return False, f"cn_ratio_low({cn_ratio:.2f})"

    printable = len(_PRINTABLE_PATTERN.findall(stripped))
    garbled_ratio = 1 - printable / max(len(stripped), 1)
    if garbled_ratio > MAX_GARBLED_RATIO:
        return False, f"garbled({garbled_ratio:.2f})"

    return True, "ok"
