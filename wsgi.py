#!/usr/bin/python3
"""gunicorn 入口：`gunicorn wsgi:app`。

web/app.py 自己会把项目根目录插进 sys.path，所以这里只要把 web/ 也放进去再 import。
注意：**不要用 gunicorn --preload**——app.py 在导入时就建了 SQLite 连接，
preload 会让同一个连接被 fork 到多个 worker，SQLite 明确不支持跨进程共享连接。
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "web"))

from app import app  # noqa: E402

if __name__ == "__main__":          # 直接 python3 wsgi.py 时也能跑（调试用）
    app.run(host="127.0.0.1", port=3009, debug=False)
