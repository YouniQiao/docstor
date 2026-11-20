"""GitCode OAuth 2.0 登录（仅用于「人工忽略问题」的身份识别）。

凭据一律来自项目根目录的 .gitcode.env（600、git-ignored），代码里不写死任何值；
文件缺失或键不齐时整体降级为「登录未配置」——顶栏按钮自动隐藏，接口返回 503，
任何情况下都不抛 500。

路由：
  GET /auth/login     生成 state 存 session → 302 到 GitCode 授权页
  GET /auth/callback  校验 state → code 换 token → 取用户 → 写 session → 回 /issues
  GET /auth/logout    清 session → 回 /issues
  GET /api/me         当前登录用户（未登录返回 {user: null}）

GitCode OAuth 流程（https://docs.gitcode.com/docs/apis/oauth）：
  授权   GET  {AUTHORIZE_URL}?client_id&redirect_uri&response_type=code&scope&state
  换令牌 POST {TOKEN_URL}?grant_type=authorization_code&code&client_id
         client_secret 放 body（form）
  取用户 GET  {USERINFO_URL}  Authorization: Bearer <token>
"""

from __future__ import annotations

import json
import secrets
import urllib.parse
from datetime import timedelta
from pathlib import Path

import requests
from flask import jsonify, redirect, request, session

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".gitcode.env"

_DEFAULTS = {
    "GITCODE_AUTHORIZE_URL": "https://gitcode.com/oauth/authorize",
    "GITCODE_TOKEN_URL": "https://gitcode.com/oauth/token",
    "GITCODE_USERINFO_URL": "https://api.gitcode.com/api/v5/user",
    "GITCODE_REDIRECT_URI": "https://docstor.openharmony.cool/auth/callback",
    "GITCODE_SCOPE": "all_user",
}
_REQUIRED = ("GITCODE_CLIENT_ID", "GITCODE_CLIENT_SECRET")
_HTTP_TIMEOUT = 15
_SESSION_DAYS = 30


def load_env(path: Path = ENV_PATH) -> dict:
    """解析 .gitcode.env（KEY=value，# 注释，值可带引号）。"""
    env: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return env
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            env[k] = v
    return env


def config() -> dict:
    env = {**_DEFAULTS, **load_env()}
    env["_enabled"] = all(env.get(k) for k in _REQUIRED)
    return env


def auth_enabled() -> bool:
    return config()["_enabled"]


def current_user() -> dict | None:
    u = session.get("user")
    return u if isinstance(u, dict) and u.get("login") else None


def login_name() -> str:
    """用于记录「谁忽略的」：GitCode 登录名（拿不到时退回昵称/id）。"""
    u = current_user() or {}
    return u.get("login") or u.get("name") or ""


def _fail(msg: str, code: int = 400):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": msg}), code
    # msg 里可能带 GitCode 回传的 error_description（攻击者可控），必须转义后再拼 HTML
    from markupsafe import escape
    return (f"<p style='font:14px/1.7 system-ui;padding:40px'>{escape(msg)}</p>", code)


def register_auth(app) -> None:
    @app.before_request
    def _session_lifetime():  # noqa: D401
        session.permanent = True

    @app.get("/api/me")
    def api_me():
        cfg = config()
        return jsonify({"user": current_user(), "enabled": cfg["_enabled"]})

    @app.get("/auth/login")
    def auth_login():
        cfg = config()
        if not cfg["_enabled"]:
            return _fail("GitCode 登录未配置：请创建 .gitcode.env（CLIENT_ID / CLIENT_SECRET）。", 503)
        state = secrets.token_urlsafe(24)
        session["oauth_state"] = state
        nxt = request.args.get("next", "")
        session["after_login"] = nxt if nxt.startswith("/") and not nxt.startswith("//") else ""
        q = {
            "client_id": cfg["GITCODE_CLIENT_ID"],
            "redirect_uri": cfg["GITCODE_REDIRECT_URI"],
            "response_type": "code",
            "scope": cfg.get("GITCODE_SCOPE") or "all_user",
            "state": state,
        }
        return redirect(cfg["GITCODE_AUTHORIZE_URL"] + "?" + urllib.parse.urlencode(q))

    @app.get("/auth/callback")
    def auth_callback():
        cfg = config()
        if not cfg["_enabled"]:
            return _fail("GitCode 登录未配置。", 503)
        code = request.args.get("code", "")
        state = request.args.get("state", "")
        want = session.pop("oauth_state", None)
        if not code:
            return _fail(f"授权失败：{request.args.get('error_description') or '没有拿到 code'}", 400)
        if not want or state != want:
            return _fail("state 校验失败，请重新登录。", 400)
        try:
            tok = requests.post(
                cfg["GITCODE_TOKEN_URL"],
                params={"grant_type": "authorization_code", "code": code,
                        "client_id": cfg["GITCODE_CLIENT_ID"],
                        "redirect_uri": cfg["GITCODE_REDIRECT_URI"]},
                data={"client_secret": cfg["GITCODE_CLIENT_SECRET"]},
                timeout=_HTTP_TIMEOUT, headers={"Accept": "application/json"})
            payload = tok.json() if tok.content else {}
        except Exception as e:  # noqa: BLE001
            app.logger.warning("gitcode token 请求失败: %s", e)
            return _fail("换 token 失败（网络异常），请重试。", 502)
        token = payload.get("access_token")
        if not token:
            return _fail(f"换 token 失败：{json.dumps(payload, ensure_ascii=False)[:200]}", 400)
        try:
            r = requests.get(cfg["GITCODE_USERINFO_URL"], timeout=_HTTP_TIMEOUT,
                             headers={"Authorization": f"Bearer {token}",
                                      "Accept": "application/json"})
            u = r.json() if r.content else {}
        except Exception as e:  # noqa: BLE001
            app.logger.warning("gitcode userinfo 请求失败: %s", e)
            return _fail("获取 GitCode 用户信息失败，请重试。", 502)
        login = u.get("login") or u.get("username") or ""
        if not login:
            return _fail("GitCode 未返回用户名，无法登录。", 400)
        session["user"] = {
            "login": login,
            "name": u.get("name") or login,
            "id": str(u.get("id") or ""),
            "avatar": u.get("avatar_url") or "",
        }
        session.permanent = True
        return redirect(session.pop("after_login", "") or "/issues")

    @app.get("/auth/logout")
    def auth_logout():
        session.pop("user", None)
        return redirect("/issues")


def init_app(app) -> None:
    """由 web/app.py 调用：注册路由 + 设置会话有效期。"""
    app.permanent_session_lifetime = timedelta(days=_SESSION_DAYS)
    register_auth(app)
