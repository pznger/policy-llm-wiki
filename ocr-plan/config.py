"""全局配置。"""
from __future__ import annotations

from pathlib import Path

# ------------------ 路径 ------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = PROJECT_ROOT / "raw-0"
RAW_DIR.mkdir(exist_ok=True, parents=True)

# 多模态 LLM (OpenAI 兼容)
LLM_BASE_URL = "https:"
LLM_API_KEY = "sk-"

# 默认多模态模型；必须支持 image_url 输入。
# 可选示例：qwen-vl-plus / qwen-vl-max / gemini-2.5-flash / gpt-4o
MULTIMODAL_MODEL = "qwen3.5-flash"

# ------------------ 质量阈值 ------------------
# 单页正文最少字符数（小于此值则判定本层提取失败，触发回退）
MIN_CHARS_PER_PAGE = 30
# 单页中文字符占非空白字符比例下限（防止提取出乱码）
MIN_CN_RATIO = 0.30
# 单页非可打印字符占比上限（乱码检测）
MAX_GARBLED_RATIO = 0.15

# 一页中若图片面积占比超过此值，则触发多模态 LLM 描述（避免对所有小图都调用）
IMAGE_AREA_RATIO_FOR_VLM = 0.05

# ------------------ 表格检测 ------------------
# 表格至少要有这么多行 / 列
TABLE_MIN_ROWS = 2
TABLE_MIN_COLS = 2
# 单 cell 超过这个字符数，大概率是正文段落被误判
TABLE_MAX_CELL_CHARS = 200
# 单 cell 平均字符数上限（保护：通栏正文常 > 这个值）
TABLE_MAX_AVG_CELL_CHARS = 80
# 非空 cell 的比例下限；过低基本是误检
TABLE_MIN_FILLED_RATIO = 0.50
# 行内列数的不一致比例上限（每行 cell 数与众数 cell 数偏离的比例）
TABLE_MAX_COL_VAR_RATIO = 0.40
# 表格区域占整页面积的下限（太小往往是装饰边框或误识别）
TABLE_MIN_AREA_RATIO = 0.02
# 表格区域占整页面积的上限（太大往往是把整页正文当成表）
TABLE_MAX_AREA_RATIO = 0.95
# 是否让多模态模型对"低置信"候选做二次确认
TABLE_VLM_CLASSIFY = True

# ------------------ Markdown 启发式 ------------------
# 一级标题最大长度（避免误把长句当标题）
H1_MAX_LEN = 40
H2_MAX_LEN = 60
H3_MAX_LEN = 80
