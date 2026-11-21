"""对外门面：其他检查项只需要 review() / review_image()。

铁律：写库（缓存 + 用量 + 结论）都在**网络请求之后**，绝不跨请求持有事务。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import pathlib

from .cache import (cache_get, cache_key, cache_put, ensure_schema, item_hash,
                    log_usage, note_put, tokens_today)
from .client import AIError, chat, chat_json
from .config import load_config
from .prompts import PROMPTS

MAX_SIDE = 1200          # 送模型前缩图，控制图片 token
_SCHEMA_READY = False


def _ready() -> None:
    global _SCHEMA_READY
    if not _SCHEMA_READY:
        ensure_schema()
        _SCHEMA_READY = True


def _img_part(path: str) -> dict:
    """图片 → data URL（缩到长边 1200、转 JPEG，避免超大 payload）。"""
    raw = pathlib.Path(path).read_bytes()
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            m = max(im.size)
            if m > MAX_SIDE:
                _resample = getattr(Image, "Resampling", Image).LANCZOS
                im = im.resize((max(1, round(im.width * MAX_SIDE / m)),
                                max(1, round(im.height * MAX_SIDE / m))), _resample)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            raw = buf.getvalue()
            mime = "image/jpeg"
    except Exception:  # noqa: BLE001
        suffix = pathlib.Path(path).suffix.lower().lstrip(".") or "png"
        mime = f"image/{suffix}"
    b64 = base64.b64encode(raw).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def review(rubric: str, fields: dict, images: list[str] | None = None, *,
           module: str = "", item_key: str = "", tier: str = "fast",
           use_cache: bool = True, max_tokens: int = 2000) -> dict:
    """按 rubric 做一次结构化判定。返回 verdict/confidence/reason/suggestion + _meta。"""
    spec = PROMPTS.get(rubric)
    if not spec:
        raise AIError(f"未知 rubric: {rubric}")
    _ready()
    cfg = load_config()
    if not cfg.configured:
        raise AIError("AI 未配置")
    if cfg.daily_token_budget and tokens_today() >= cfg.daily_token_budget:
        raise AIError(f"已达每日 token 预算（{cfg.daily_token_budget}），今日不再调用 AI")
    model, pver = cfg.model(tier), spec["version"]

    imgs_meta = []
    for p in (images or []):
        try:
            h = hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
        except OSError:
            h = ""
        imgs_meta.append({"hash": h})
    payload = {"fields": fields, "images": imgs_meta}
    key = cache_key(rubric, model, pver, payload)

    if use_cache:
        hit = cache_get(key)
        if hit is not None:
            out = dict(hit)
            out["_meta"] = {"cached": True, "model": model, "prompt_version": pver, "tokens": 0}
            return out

    user_txt = spec["user"].format(**{k: (fields.get(k) or "") for k in
                                      ("doc_url", "doc_title", "ocr_text", "han", "ratio")})
    content: list = [{"type": "text", "text": user_txt}]
    for p in (images or []):
        content.append(_img_part(str(p)))
    msgs = [{"role": "system", "content": spec["system"]},
            {"role": "user", "content": content}]

    t0 = time.time()
    try:
        raw, usage = chat_json(msgs, model, max_tokens=max_tokens)
    except AIError as ex:
        log_usage(rubric, module, item_key, model, 0, 0,
                  int((time.time() - t0) * 1000), ok=False, error=str(ex))
        raise
    out = {"verdict": raw.get("verdict", "uncertain"),
           "confidence": raw.get("confidence"),
           "reason": (raw.get("reason") or "").strip()[:400],
           "suggestion": (raw.get("suggestion") or "").strip()[:200]}
    tin = int(usage.get("prompt_tokens", 0) or 0)
    tout = int(usage.get("completion_tokens", 0) or 0)
    if use_cache:
        cache_put(key, rubric, model, pver, item_hash(payload), out, usage)
    log_usage(rubric, module, item_key, model, tin, tout,
              int((time.time() - t0) * 1000), ok=True)
    if module and item_key:
        note_put(module, item_key, rubric, out, model, pver)
    out["_meta"] = {"cached": False, "model": model, "prompt_version": pver, "tokens": tout}
    return out


def review_image(rubric: str, fields: dict, image: str, **kw) -> dict:
    return review(rubric, fields, images=[image], **kw)
