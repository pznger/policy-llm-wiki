# scripts/

辅助 wiki 维护的 Python 脚本。脚本只负责确定性的"机械操作"，**摘要、关联、表格修复等需要语义判断的工作仍由 LLM agent 完成**。

## split_wiki.py

把 `raw/<规范名>.md` 按"章 / 节"拆分到 `wiki/<规范名>/`，并为每个正文文件生成占位 `-index.md`。

```bash
python scripts/split_wiki.py raw/药物警戒质量管理规范.md
```

可选参数：

- `--output-root <dir>`：默认 `wiki`。
- `--max-pages-per-section <N>`：单个章或节最大页数，超过则继续按 `-1/-2/...` 切分，默认 **2**（ccic 建筑规范默认 5；政策库以 `##` 为文件单元，2 页内不再拆）。

行为概览（详见 `wiki-拆分指导.md`）：

- `# 第X章 ...`、`# 附则`、`# 附录X ...`、`# 附件X ...` 视为章。
- `## 第X节 ...`、`## X.X ...` 视为节。
- `第X条`、`X.X.X` 一律视为正文，不参与拆分。
- 整篇没有"章"时，把整个文件当成单一 wiki 文件。
- `#` 为章目录，`##` 为章内正文文件（与 ccic 一致）；章下没有 `##` 时以章为单文件（如 `总则.md`）。
- 章或节超过页数上限（默认 2 页）时按页切 `-1/-2`，相邻段保留 1 页重叠，续段补回当前节标题。
- 正文 `-index.md`：`条目录` 仅 **条号（第N页）**；`条文摘要` 每条一条 TODO（补写 **约 50 字**、1～2 句）；无章指南按页拆 `-1`、`-2`…
- 页码标记 `<!-- 第 N 页 -->` 表示**第 N 页结束**，其后内容为第 N+1 页（与 ocr-plan 一致）。

## count_split_lines.py

粗略对比"源 markdown 行数"与"拆分后非 `-index.md` 文件总行数"，用来快速发现拆分丢内容或重复过多。

```bash
python scripts/count_split_lines.py raw/药物警戒质量管理规范.md
python scripts/count_split_lines.py raw/药物警戒质量管理规范.md --details
```

经验值：拆分后总行数比源文件少 5% ～ 20% 通常正常；明显超出该区间或反向暴增需要检查。

## check_wiki_page_numbers.py

对照 `raw/` 重算条号→页码，检查各 `*-index.md` 中「（第N页）」「（第N～M页）」是否与之一致（**不改文件**）。

```bash
python scripts/check_wiki_page_numbers.py
python scripts/check_wiki_page_numbers.py --only 医疗器械监督管理条例
python scripts/check_wiki_page_numbers.py --report qa-test/_page_check_report.md
```

退出码：有不一致时为 `1`，全部 OK 为 `0`。

## fix_wiki_page_numbers.py

**不重拆 wiki**：只根据 `raw/` 用修正后的页码语义，更新各 `*-index.md` 里的「（第N页）」「（第N～M页）」；**正文 `.md` 与已写好的条文摘要正文不动**。

适用：曾用旧版 `split_wiki.py`（把 `<!-- 第 N 页 -->` 当成页首）导致条目录页码整体偏早 1 页。

```bash
# 先看会改多少处
python scripts/fix_wiki_page_numbers.py --dry-run

# 全库修正
python scripts/fix_wiki_page_numbers.py

# 只修一部
python scripts/fix_wiki_page_numbers.py --only 医疗器械监督管理条例
```

## batch_split_wiki.py

批量处理 `raw/` 下全部 `.md`（跳过 `_` 开头文件），输出到 `wiki/<规范名>/`。

```bash
python scripts/batch_split_wiki.py
python scripts/batch_split_wiki.py --dry-run
python scripts/batch_split_wiki.py --only 药物警戒质量管理规范.md 医疗器械监督管理条例.md
python scripts/batch_split_wiki.py --report
```

可选参数：`--raw-dir`、`--output-root`、`--max-pages-per-section`（同 `split_wiki.py`）。加 `--report` 会在 `wiki/_batch_split_report.md` 写入行数比值汇总。

## 典型工作流

```bash
# 1. 人工确认 raw/<规范名>.md 已校验
# 2. 批量拆分（或单份：split_wiki.py）
python scripts/batch_split_wiki.py --report
# 3. 行数对比（抽查）
python scripts/count_split_lines.py raw/药物警戒质量管理规范.md
# 4. 让 LLM 按 wiki-拆分指导.md §7 补全摘要（§4）与关联（§5）
```
