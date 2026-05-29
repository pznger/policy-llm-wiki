# Markdown 清洗流水线

把 `raw-0/` 下 OCR/抽取得到的"毛刺较多"的 markdown，清洗成 `clean/` 下规范的 markdown。

## 解决的问题

1. **标题层级混乱**：仅 `第X章` 为一级、`第X节`/`X.X` 为二级；`第X条`、`2.3.1` 等应为正文，不应带 `#`
2. **标题中文有多余空格**：`第 四 条` / `第二十 一 条` / `# 第一章 总 则`
3. **加粗错误**：`**6**`、`**第三十七条**` 之类不该加粗的字段被加黑
4. **残留页脚页码**：除了 `<!-- 第 N 页 -->`，正文里还混着 `- 1 -` / 单独成行的纯数字
5. **行内多余空格**：`生产 企业、 经营企业`

## 设计

整体两段式：

```
原始 .md
  ↓ rules.py   本地规则清洗（不调用 LLM）
  ↓ chunker.py 按 <!-- 第 N 页 --> 切页，并把表格/图片/HTML注释替换成占位符
  ↓ cleaner.py 把每页正文送到 LLM
  ↓           再把占位符还原
干净 .md
```

- **本地规则**先把能用代码确定性修掉的都修掉（孤立页码行、行内多余空格、加粗在中文条款编号上等），尽量省 token、避免 LLM 改坏
- **占位符保护**：表格 / 图片 / HTML 注释会先替换为 `<<TAB_0>>` / `<<IMG_0>>` / `<<CMT_0>>`，再让 LLM 处理，最后无损还原
- **按页分块**：每页一次 LLM 调用，避免长上下文导致漂移；同时每页的 `<!-- 第 N 页 -->` 标签会原样保留

## 用法

```bash
conda activate policy-wiki

# 默认：读 raw-0/，写到 clean/
python clean-plan/main.py

# 处理单个文件
python clean-plan/main.py --file raw-0/医疗器械生产监督管理办法.md

# 跳过 LLM，只跑本地规则
python clean-plan/main.py --rules-only

# 覆盖已存在的产物
python clean-plan/main.py --overwrite

# 控制并发：一次给 N 个文件并行调用 LLM
python clean-plan/main.py --workers 4
```

运行结束会在 `clean/_clean_report.md` 输出 markdown 格式的清洗汇总（每个文件改动了多少处、每页 LLM 调用是否成功、耗时等）。
