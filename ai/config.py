"""读 AI 配置：项目根 .ai.env（600，gitignore）。内容哈希变化自动重载，改 key 不用重启。"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".ai.env"


def _read_env(path: pathlib.Path) -> dict[str, str]:
    d: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


@dataclass
class AIConfig:
    base_url: str = ""
    api_key: str = ""
    model_fast: str = "deepseek-v4.1-flash"
    model_smart: str = "deepseek-v4.1-flash"
    extra_headers: dict = field(default_factory=dict)
    timeout: float = 90.0
    max_retries: int = 3
    daily_token_budget: int = 3_000_000     # 全站每日 token 上限，超了自动降级为「不调 AI」
    db_path: str = str(ROOT / "data" / "docstor.db")

    def model(self, tier: str = "fast") -> str:
        return self.model_smart if tier == "smart" else self.model_fast

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


_cfg: AIConfig | None = None
_SIG = ""


def _signature() -> str:
    try:
        return hashlib.sha256(ENV_FILE.read_bytes()).hexdigest()
    except OSError:
        return ""


def key_fingerprint() -> str:
    """当前 key 的短指纹（缓存归属判断，不泄露 key）。"""
    return hashlib.sha256(load_config().api_key.encode()).hexdigest()[:12]


def load_config(reload: bool = False) -> AIConfig:
    global _cfg, _SIG
    sig = _signature()
    if _cfg is not None and not reload and sig == _SIG:
        return _cfg
    e = _read_env(ENV_FILE)
    headers: dict = {}
    if e.get("AI_EXTRA_HEADERS"):
        try:
            headers = json.loads(e["AI_EXTRA_HEADERS"])
        except Exception:  # noqa: BLE001
            headers = {}
    _cfg = AIConfig(
        base_url=e.get("AI_BASE_URL", "").rstrip("/"),
        api_key=e.get("AI_API_KEY", ""),
        model_fast=e.get("AI_MODEL_FAST", "deepseek-v4.1-flash"),
        model_smart=e.get("AI_MODEL_SMART", e.get("AI_MODEL_FAST", "deepseek-v4.1-flash")),
        extra_headers=headers,
        timeout=float(e.get("AI_TIMEOUT", "90")),
        max_retries=int(e.get("AI_MAX_RETRIES", "3")),
        daily_token_budget=int(e.get("AI_DAILY_TOKEN_BUDGET", "3000000")),
    )
    _SIG = sig
    return _cfg
