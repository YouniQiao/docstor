"""检查项注册入口。新增检查项：加一行 import 即可。"""
from . import base  # noqa: F401
from . import link_check   # noqa: F401  link.dead / link.anchor / link.vintage / link.cross_lang
from . import special_check   # noqa: F401  special.agc_name（专项检查）
from . import image_check   # noqa: F401  image.missing（图片检查）
from . import image_zh_in_en  # noqa: F401  image.zh_in_en（英文图含中文，OCR+AI）
from .base import REGISTRY, Issue, DocContext, applicable, rule_label  # noqa: F401

__all__ = ["REGISTRY", "Issue", "DocContext", "applicable", "rule_label"]
