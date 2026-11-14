"""检查项注册入口。新增检查项：加一行 import 即可。"""
from . import base  # noqa: F401
from . import link_check   # noqa: F401  link.dead / link.anchor / link.vintage / link.cross_lang
from . import text_check   # noqa: F401  text.dup / text.typo
from . import image_check  # noqa: F401  image.blur / image.cn_text
from .base import REGISTRY, Issue, DocContext, applicable, rule_label  # noqa: F401

__all__ = ["REGISTRY", "Issue", "DocContext", "applicable", "rule_label"]
