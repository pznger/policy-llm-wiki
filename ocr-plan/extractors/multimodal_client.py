"""多模态 LLM 客户端：把图片送给视觉大模型，拿回中文描述。

接口走 OpenAI 兼容协议（/v1/chat/completions），baseurl 与 apikey 见 config.py。
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from loguru import logger
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from config import LLM_API_KEY, LLM_BASE_URL, MULTIMODAL_MODEL

_PROMPT = (
    "请用简体中文客观描述这张图片的内容。"
    "如果是表格、流程图、公式或示意图，请说明结构与关键文字；"
    "如果是装饰性图标、空白或无意义内容，请直接回复『（无关图片）』。"
    "控制在 120 字以内。"
)


class MultiModalClient:
    """对接多模态 LLM 的轻量客户端，内置调用计数。"""

    def __init__(
        self,
        base_url: str = LLM_BASE_URL,
        api_key: str = LLM_API_KEY,
        model: str = MULTIMODAL_MODEL,
    ) -> None:
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self.total_calls: int = 0
        self.success_calls: int = 0
        self.failed_calls: int = 0

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _to_data_url(image_path: str | Path) -> str:
        p = Path(image_path)
        mime, _ = mimetypes.guess_type(p.name)
        if mime is None:
            mime = "image/png"
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=False,
    )
    def _call(self, data_url: str, prompt: str, max_tokens: int = 300) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    def describe_image(
        self,
        image_path: str | Path,
        prompt: str | None = None,
        max_tokens: int = 300,
    ) -> str:
        """对单张图片生成描述；失败时返回空字符串而不抛出。"""
        self.total_calls += 1
        try:
            data_url = self._to_data_url(image_path)
            text = self._call(data_url, prompt or _PROMPT, max_tokens=max_tokens)
            self.success_calls += 1
            logger.debug(f"[VLM] OK {Path(image_path).name} -> {len(text)} 字")
            return text
        except Exception as e:  # noqa: BLE001
            self.failed_calls += 1
            logger.warning(f"[VLM] 失败 {Path(image_path).name}: {e}")
            return ""

    def is_table(self, image_path: str | Path) -> bool | None:
        """判断截图是否为表格。返回 True/False；调用失败返回 None。"""
        prompt = (
            "图片是否为一个真正的表格（具有规则的行列结构，而不是普通正文段落、列表或目录）？"
            "请只回答 yes 或 no，不要其他任何文字。"
        )
        self.total_calls += 1
        try:
            data_url = self._to_data_url(image_path)
            text = self._call(data_url, prompt, max_tokens=4).strip().lower()
            self.success_calls += 1
            logger.debug(f"[VLM] is_table {Path(image_path).name} -> {text!r}")
            if text.startswith("y"):
                return True
            if text.startswith("n"):
                return False
            return None
        except Exception as e:  # noqa: BLE001
            self.failed_calls += 1
            logger.warning(f"[VLM] is_table 失败 {Path(image_path).name}: {e}")
            return None
