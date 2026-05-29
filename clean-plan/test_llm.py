"""快速测试 LLM 是否能正常返回。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai import OpenAI

from config import CLEAN_MODEL, LLM_API_KEY, LLM_BASE_URL, LLM_TIMEOUT


def main() -> None:
    print(f"base_url = {LLM_BASE_URL}")
    print(f"model    = {CLEAN_MODEL}")
    print(f"timeout  = {LLM_TIMEOUT}s")
    print("-" * 50)

    client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY, timeout=LLM_TIMEOUT)

    prompt = "请把下面这段清洗一下：\n\n第 四 条 国家鼓励 医疗 器械研制。\n\n直接返回清洗后的文本，不要解释。"

    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=CLEAN_MODEL,
            messages=[
                {"role": "system", "content": "你是一个中文文本清洗助手，只输出清洗结果。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        elapsed = time.time() - t0
        text = resp.choices[0].message.content
        usage = resp.usage
        print(f"[OK] 耗时 {elapsed:.1f}s")
        print(f"     prompt_tokens={getattr(usage, 'prompt_tokens', '-')} "
              f"completion_tokens={getattr(usage, 'completion_tokens', '-')}")
        print("-" * 50)
        print("模型返回：")
        print(text)
    except Exception as e:  # noqa: BLE001
        elapsed = time.time() - t0
        print(f"[FAIL] 耗时 {elapsed:.1f}s")
        print(f"       错误：{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
