"""auth.py 跟 /healthz endpoint 的單元測試
驗證：
- 沒設 BASIC_AUTH_USER/PASS → 完全 passthrough
- 有設 → 401 Challenge / 401 wrong creds / 200 right creds
- /healthz 永遠 200（不經過 auth）
- compare_digest 防 timing attack（用對的密碼對的格式）
"""
import base64
import importlib
import os
import sys

import pytest


@pytest.fixture
def auth_module(monkeypatch):
    """每個 test 獨立載入 auth module（讓環境變數變化生效）"""
    # 不要污染全域 — 用 importlib.reload
    if "auth" in sys.modules:
        del sys.modules["auth"]
    import auth
    return auth


class TestAuthIsEnabled:
    def test_no_env_disabled(self, auth_module, monkeypatch):
        monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
        monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
        # 重載讓變化生效
        import auth
        importlib.reload(auth)
        assert auth.is_enabled() is False

    def test_only_user_set_disabled(self, monkeypatch):
        monkeypatch.setenv("BASIC_AUTH_USER", "admin")
        monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
        import auth
        importlib.reload(auth)
        assert auth.is_enabled() is False

    def test_both_set_enabled(self, monkeypatch):
        monkeypatch.setenv("BASIC_AUTH_USER", "admin")
        monkeypatch.setenv("BASIC_AUTH_PASS", "p@ssw0rd")
        import auth
        importlib.reload(auth)
        assert auth.is_enabled() is True


def _basic_auth_header(user: str, pwd: str) -> dict:
    token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class TestAuthMiddleware:
    """用 FastAPI TestClient 模擬 HTTP 請求"""

    def _make_app(self, monkeypatch, user: str | None = None, pwd: str | None = None):
        """建一個只有 auth + 兩個簡單 endpoint 的測試 app"""
        if user is None:
            monkeypatch.delenv("BASIC_AUTH_USER", raising=False)
        else:
            monkeypatch.setenv("BASIC_AUTH_USER", user)
        if pwd is None:
            monkeypatch.delenv("BASIC_AUTH_PASS", raising=False)
        else:
            monkeypatch.setenv("BASIC_AUTH_PASS", pwd)

        # 重載 auth 讓環境變數生效
        import auth
        importlib.reload(auth)

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.middleware("http")(auth.basic_auth_middleware)

        @app.get("/healthz")
        def hz():
            return {"status": "ok"}

        @app.get("/api/v1/test")
        def test_endpoint():
            return {"data": "secret"}

        return TestClient(app)

    def test_disabled_passthrough(self, monkeypatch):
        client = self._make_app(monkeypatch)  # no env
        r = client.get("/api/v1/test")
        assert r.status_code == 200
        assert r.json() == {"data": "secret"}

    def test_enabled_no_creds_returns_401(self, monkeypatch):
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/api/v1/test")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers
        assert "Basic" in r.headers["WWW-Authenticate"]

    def test_enabled_wrong_password(self, monkeypatch):
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/api/v1/test", headers=_basic_auth_header("admin", "wrong"))
        assert r.status_code == 401

    def test_enabled_wrong_user(self, monkeypatch):
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/api/v1/test", headers=_basic_auth_header("hacker", "secret"))
        assert r.status_code == 401

    def test_enabled_correct_creds(self, monkeypatch):
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/api/v1/test", headers=_basic_auth_header("admin", "secret"))
        assert r.status_code == 200
        assert r.json() == {"data": "secret"}

    def test_healthz_bypasses_auth(self, monkeypatch):
        """Cloud Run 健康檢查不能被 auth 擋"""
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_malformed_auth_header(self, monkeypatch):
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/api/v1/test", headers={"Authorization": "Bearer xxx"})
        assert r.status_code == 401

    def test_invalid_base64(self, monkeypatch):
        client = self._make_app(monkeypatch, user="admin", pwd="secret")
        r = client.get("/api/v1/test", headers={"Authorization": "Basic !!!notbase64"})
        assert r.status_code == 401


class TestServerHealthz:
    """server.py 真實 app 的 /healthz 測試"""

    def test_healthz_200(self):
        # 確保沒有 BASIC_AUTH 環境變數，避免影響其他 server 測試
        for k in ("BASIC_AUTH_USER", "BASIC_AUTH_PASS"):
            os.environ.pop(k, None)
        # reload auth + server 確保拿到乾淨狀態
        for mod in ("auth", "server"):
            if mod in sys.modules:
                del sys.modules[mod]
        import server
        from fastapi.testclient import TestClient
        client = TestClient(server.app)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
