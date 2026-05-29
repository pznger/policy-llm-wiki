# qa-test

政策法规 wiki 的问答评测，流程对齐 [ccic-llm-wiki](../ccic-llm-wiki/qa-test/)，并强化 **原文摘抄溯源** 以降低幻觉。

## 文件

| 文件 | 用途 |
| --- | --- |
| `question-template.md` | 单题答题模板（**先溯源、后答案**） |
| `answer-example.md` | 格式示例 |
| `generate-questions.md` | 从模板生成 `answer-x.md` |
| `answer-by-subagent.md` | 多 subagent 批量答题 |
| `evaluate.md` | 对照 `correct-answers.md` 与 wiki 摘抄评估 |
| `check-answer-against-wiki.md` | 仅根据 wiki 核对某份答案是否可溯源 |

## 推荐流程

```text
1. 用 question-template.md 生成 answer-x.md
2. Agent 只读 wiki/ 作答（溯源表 §6.2；归纳答案 §6.4，见 `wiki-查询指导.md`）
3. evaluate.md 或 check-answer-against-wiki.md 核验
4. 可选：维护 correct-answers.md（仅放已在 wiki 中可逐字溯源的标准答案）
```

## 与 ccic 的差异

- 条号多用「第×条」或指南「2.1.1」，须写 **页码**（`<!-- 第 N 页 -->`）。
- 跨规范转引多（办法 → 条例），须核对 **被引规范在 wiki 中的实际条文**，不能只看转引条号。
- 查询规则详见 [`wiki-查询指导.md`](../wiki-查询指导.md) §6。
