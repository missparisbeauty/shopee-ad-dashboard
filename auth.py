"""
HTTP Basic Auth middleware（給 Cloud Run 部署時保護）
─────────────────────────
有設 BASIC_AUTH_USER + BASIC_AUTH_PASS 環境變數時啟用。
本機開發不設環境變數 → 不啟用認證。

啟用方式（Cloud Run）:
    gcloud run deploy ... \
      --set-env-vars BASIC_AUTH_USER=admin,BASIC_AUTH_PASS=YourStrongPassword

安全考量：
- 用 secrets.compare_digest 防 timing attack
- 401 回應帶 WWW-Authenticate header（瀏覽器會跳出帳密輸入框）
- 只在環境變數設了才啟用 → 本機 0 影響
"""
from __future__ import annotations

import base64
import os
import secrets

from fastapi import Request
from fastapi.responses import Response


def _expected_creds() -> tuple[str, str] | None:
    user = os.environ.get("BASIC_AUTH_USER")
    pwd = os.environ.get("BASIC_AUTH_PASS")
    if not user or not pwd:
        return None
    return user, pwd


def is_enabled() -> bool:
    return _expected_creds() is not None


async def basic_auth_middleware(request: Request, call_next):
    """FastAPI middleware — 沒設 BASIC_AUTH_* 環境變數就 passthrough"""
    creds = _expected_creds()
    if creds is None:
        return await call_next(request)

    # 健康檢查 endpoint 不需認證（讓 Cloud Run 健康檢查能跑）
    if request.url.path in ("/healthz", "/api/v1/healthz"):
        return await call_next(request)

    expected_user, expected_pass = creds
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Basic "):
        return _challenge_response()

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        user, _, pwd = decoded.partition(":")
    except Exception:
        return _challenge_response()

    # 用 compare_digest 防 timing attack
    user_ok = secrets.compare_digest(user, expected_user)
    pass_ok = secrets.compare_digest(pwd, expected_pass)

    if not (user_ok and pass_ok):
        return _challenge_response()

    return await call_next(request)


def _challenge_response() -> Response:
    return Response(
        content='{"error":{"code":"AUTH_REQUIRED","message":"請輸入帳號密碼"}}',
        status_code=401,
        headers={
            # WWW-Authenticate header 必須是 ASCII（latin-1 限制）— 不能含中文
            "WWW-Authenticate": 'Basic realm="Shopee Dashboard"',
            "Content-Type": "application/json; charset=utf-8",
        },
    )
