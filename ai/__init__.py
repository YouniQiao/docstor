"""docstor AI 能力：站点级通用模块（其他检查项按需调用）。

用法：
    from ai import review_image, review, usage_summary, ai_ready

    res = review_image("image_zh_in_en",
                       fields={"doc_url": ..., "doc_title": ..., "ocr_text": ..., "meta": ...},
                       image="/abs/path/x.png",
                       module="image", item_key="<img sha256>")

返回：{"verdict": "real|false_positive|uncertain", "confidence": 0.9,
       "reason": "...", "suggestion": "...", "_meta": {"cached": False, "tokens": 312}}
"""
from __future__ import annotations

from .client import AIError
from .config import load_config
from .facade import review, review_image
from .usage import usage_summary


def ai_ready() -> bool:
    """AI 是否已配置（.ai.env 有 base_url + api_key）。"""
    return load_config().configured


__all__ = ["AIError", "ai_ready", "review", "review_image", "usage_summary", "load_config"]
