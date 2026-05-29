# 政策文档 OCR / 提取流水线

将 `data/` 下的 `pdf`、`docx`、`doc` 文档统一转换为 markdown，输出到 `raw/`。

## 1. 设计概述

### PDF 提取

| 内容 | 工具 | 用途 |
|------|------|------|
| 正文 | `pdfplumber` | 提取数字版 PDF 文本，并按表格/图片区域排除重复内容 |
| 表格 | `pdfplumber` 定位 + 多模态 LLM | 截取表格区域，由多模态模型恢复为 HTML `<table border="1">` |
| 图片 | `pdfplumber` 定位 + 多模态 LLM | 截取图片区域，由多模态模型生成中文描述 |

当前 PDF 通道只保留 `pdfplumber`，不再使用 PyMuPDF / RapidOCR 回退。

### DOCX / DOC 提取

- `.docx`：使用 `python-docx` 直接读取段落样式（Heading 1~6）、表格（含合并单元格）、列表
- `.doc`：在 Windows 上自动通过 Word COM 转换为 `.docx`，再走 docx 流水线

### Markdown 输出规范

- 多级标题：`#`、`##`、`###` …（按 docx Heading 样式 或 PDF 启发式识别）
- 表格：保留 HTML `<table border="1">` 形式，支持 rowspan / colspan
- 页码溯源：每页末尾插入 `<!-- 第 N 页 -->`
- 图片：保存到 `raw/<doc_name>_assets/`，并通过 `<!-- desc: ... -->` 注释保存多模态描述

## 2. 环境

```bash
conda create -n policy-wiki python=3.10 -y
conda activate policy-wiki
pip install -r ocr-plan/requirements.txt
```

## 3. 使用

```bash
# 处理 data/ 下全部文档
python ocr-plan/main.py

# 仅处理某个文件
python ocr-plan/main.py --file data/某文件.pdf

# 不调用多模态模型（表格会使用 pdfplumber 原始表格兜底，图片只保留截图链接）
python ocr-plan/main.py --no-vlm
```

输出会写入 `raw/<原文件名>.md`。

## 4. 配置

API key、模型名、阈值等集中在 `ocr-plan/config.py`，需要时按需调整。
