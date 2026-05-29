"""调用 LLM 做语义级清洗的核心逻辑。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from chunker import Chunk, chunk_markdown, reassemble, unmask_protected
from config import (
    CLEAN_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_TIMEOUT,
)
from prompts import build_messages
from rules import RuleStats, apply_local_rules


# ----------------- LLM 客户端 -----------------
class CleanLLM:
    """轻量封装：负责一次 chunk 的清洗调用 + 统计。"""

    def __init__(
        self,
        base_url: str = LLM_BASE_URL,
        api_key: str = LLM_API_KEY,
        model: str = CLEAN_MODEL,
        timeout: float = LLM_TIMEOUT,
    ) -> None:
        self._client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        self._model = model
        self.total_calls = 0
        self.success_calls = 0
        self.failed_calls = 0

    @property
    def model(self) -> str:
        return self._model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=15),
        reraise=True,
    )
    def _call(self, content: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=build_messages(content),
            temperature=0.0,
            max_tokens=8000,
        )
        return (resp.choices[0].message.content or "").strip()

    def clean_chunk(self, chunk: Chunk) -> tuple[str, bool, str, int]:
        """返回 (清洗后的文本，是否成功，错误信息或空串，本次新增的表格数)。"""
        if not chunk.text.strip():
            return chunk.text, True, "", 0
        self.total_calls += 1
        try:
            out = self._call(chunk.text)
            out = _strip_md_codefence(out)
            # 安全检查：占位符必须保留下来；否则保守地回退到本地结果
            for ph in chunk.placeholders:
                if ph.token not in out:
                    logger.warning(
                        f"chunk page={chunk.page_no} seq={chunk.seq}: 缺失占位符 {ph.token}，"
                        f"回退到本地规则版本"
                    )
                    self.failed_calls += 1
                    return chunk.text, False, "placeholder_missing", 0
            # LLM 可能把"被拆开的伪表格"恢复成 <table>；统计新增数量
            new_tables = _count_new_tables(chunk.text, out)
            self.success_calls += 1
            return out, True, "", new_tables
        except Exception as e:  # noqa: BLE001
            self.failed_calls += 1
            logger.warning(
                f"chunk page={chunk.page_no} seq={chunk.seq} LLM 调用失败：{e}"
            )
            return chunk.text, False, str(e), 0


# ----------------- 文档级清洗 -----------------
@dataclass
class CleanStats:
    file: str = ""
    model: str = ""
    chunks_total: int = 0
    chunks_success: int = 0
    chunks_failed: int = 0
    chunks_skipped_rules_only: int = 0
    rule_stats: RuleStats = field(default_factory=RuleStats)
    chars_before: int = 0
    chars_after: int = 0
    elapsed_sec: float = 0.0
    # LLM 阶段把"被拆开的伪表格"恢复成 <table> 的新增数量
    tables_recovered: int = 0


def clean_markdown_file(
    src: Path,
    dst: Path,
    llm: CleanLLM | None,
    rules_only: bool = False,
) -> CleanStats:
    """清洗一个 markdown 文件并写出。"""
    src = Path(src)
    dst = Path(dst)
    stats = CleanStats(file=src.name, model=(llm.model if llm else "(rules-only)"))

    t0 = time.time()
    raw = src.read_text(encoding="utf-8")
    stats.chars_before = len(raw)

    # 第 1 步：本地规则
    after_rules, rule_stats = apply_local_rules(raw)
    stats.rule_stats = rule_stats
    logger.info(
        f"[CLEAN] {src.name} 本地规则命中：页码={rule_stats.page_numbers_removed} "
        f"条={rule_stats.article_spaces_fixed} 章={rule_stats.chapter_spaces_fixed} "
        f"节={rule_stats.section_spaces_fixed} 标题H1={rule_stats.heading_h1_fixed} "
        f"H2={rule_stats.heading_h2_fixed} 降为正文={rule_stats.heading_demoted} "
        f"加粗(短)={rule_stats.bold_short_removed} 加粗(法条)={rule_stats.bold_article_removed} "
        f"中文空格={rule_stats.cn_inline_space_fixed} "
        f"硬换行合并={rule_stats.linebreaks_merged} "
        f"空行压缩={rule_stats.blank_lines_compressed}"
    )

    if rules_only or llm is None:
        # 不调用 LLM，直接写出
        dst.write_text(after_rules, encoding="utf-8")
        stats.chars_after = len(after_rules)
        stats.chunks_skipped_rules_only = 1
        stats.elapsed_sec = time.time() - t0
        logger.success(
            f"[CLEAN] {src.name} (rules-only) -> {dst.name} "
            f"({stats.chars_before} -> {stats.chars_after}, {stats.elapsed_sec:.1f}s)"
        )
        return stats

    # 第 2 步：分块 + LLM
    chunks = chunk_markdown(after_rules)
    stats.chunks_total = len(chunks)
    logger.info(f"[CLEAN] {src.name} 切分为 {len(chunks)} 个 chunk 送 LLM")

    processed: list[tuple[Chunk, str]] = []
    for idx, ch in enumerate(chunks, 1):
        if not ch.text.strip():
            processed.append((ch, ch.text))
            continue
        out, ok, err, new_tables = llm.clean_chunk(ch)
        if ok:
            stats.chunks_success += 1
            stats.tables_recovered += new_tables
            extra = f" 恢复表格={new_tables}" if new_tables else ""
            logger.info(
                f"[CLEAN] {src.name} chunk {idx}/{len(chunks)} page={ch.page_no} seq={ch.seq} OK{extra}"
            )
        else:
            stats.chunks_failed += 1
            logger.warning(
                f"[CLEAN] {src.name} chunk {idx}/{len(chunks)} page={ch.page_no} seq={ch.seq} FAIL: {err}"
            )
        processed.append((ch, out))

    # 第 3 步：组装 + 还原占位符
    final_text = reassemble(processed)
    dst.write_text(final_text, encoding="utf-8")
    stats.chars_after = len(final_text)
    stats.elapsed_sec = time.time() - t0
    logger.success(
        f"[CLEAN] {src.name} 完成 -> {dst.name} "
        f"({stats.chars_before} -> {stats.chars_after}, "
        f"LLM ok/fail={stats.chunks_success}/{stats.chunks_failed}, "
        f"恢复表格={stats.tables_recovered}, "
        f"耗时 {stats.elapsed_sec:.1f}s)"
    )
    return stats


# ----------------- 工具 -----------------
_CODEFENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n([\s\S]*?)\n```[\s]*$", re.MULTILINE)
_TABLE_OPEN = re.compile(r"<table\b", re.IGNORECASE)


def _strip_md_codefence(text: str) -> str:
    """有些模型会把回答整段包在 ```markdown ... ``` 里，剥掉外层。"""
    m = _CODEFENCE.match(text.strip())
    if m:
        return m.group(1)
    return text


def _count_new_tables(before: str, after: str) -> int:
    """统计 LLM 清洗后新增的 ``<table>`` 数量。

    注意：原文中已识别的表格在送给 LLM 前已被替换为 ``<<TAB_X>>`` 占位符，
    所以 ``before`` 里的 ``<table`` 数通常为 0；这里保留差值以保证健壮。
    """
    before_n = len(_TABLE_OPEN.findall(before))
    after_n = len(_TABLE_OPEN.findall(after))
    return max(0, after_n - before_n)
