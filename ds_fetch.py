"""带「总时长」上限的 HTTP 请求（抓取用）。

requests 的 timeout 只管「连接」和「单次读」：服务器一点点吐字节、或中间设备
把连接挂住时，请求可以永远不返回。2026-11-20 en/getting-started 的抓取就是
这样卡死了两个 60 分钟窗口（两次都被 daily_sync 的 3600s 超时杀掉，整个板块跳过）。
这里改成「边读边看表」，超过 total 秒直接抛 DeadlineExceeded，调用方记失败继续。
"""
from __future__ import annotations

import json
import time


class DeadlineExceeded(TimeoutError):
    """总时长超了（区别于单次读超时）。"""


def _read(resp, deadline: float, chunk: int = 65536) -> bytes:
    buf = bytearray()
    for part in resp.iter_content(chunk):
        buf += part
        if time.time() > deadline:
            raise DeadlineExceeded("超过总时长上限")
    return bytes(buf)


def get(session, url: str, *, total: float = 45, connect: float = 10,
        read: float = 15) -> tuple[int, bytes, dict]:
    """GET → (status, body, headers)，总耗时不超过 total + read 秒。"""
    deadline = time.time() + total
    with session.get(url, timeout=(connect, read), stream=True) as r:
        return r.status_code, _read(r, deadline), dict(r.headers)


def post_json(session, url: str, payload: dict, *, total: float = 90,
              connect: float = 10, read: float = 20) -> dict:
    """POST JSON → 解析后的 dict，总耗时不超过 total + read 秒。"""
    deadline = time.time() + total
    with session.post(url, json=payload, timeout=(connect, read), stream=True) as r:
        body = _read(r, deadline)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return json.loads(body.decode("utf-8", "replace") or "{}")
