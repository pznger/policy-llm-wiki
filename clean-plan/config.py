"""Markdown 清洗配置。"""
from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_DIR = PROJECT_ROOT / "raw-0"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "clean"
DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 复用 ocr-plan 的同一套 OpenAI 兼容接口
LLM_BASE_URL = "https:"
LLM_API_KEY = "sk-"

# 清洗用的纯文本（非多模态）模型；按公司提供的实际模型名替换。
# 常见可选：Qwen2.5-72B-Instruct / Qwen2.5-32B-Instruct / DeepSeek-V3 / GLM-4-Plus 等
CLEAN_MODEL = "deepseek-v4-pro"

# LLM 调用上限：单次发给模型的页内字符数；超出会进一步切分
MAX_CHARS_PER_REQUEST = 6000

# 并发：同时给多少个文件做 LLM 清洗
DEFAULT_WORKERS = 1

# LLM 调用超时（秒）
LLM_TIMEOUT = 120
