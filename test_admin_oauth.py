# -*- coding: utf-8 -*-
"""admin_oauth 管理端点测试（design D8）。

覆盖口令门禁（401/403）、start 响应白名单、会话与单管理会话上限 409、
跨管理会话访问 409、poll 状态机与 ticket 兜底、import-callback URL
白名单与两阶段、logout，以及 502/503 错误契约。

沿用 test_admin_ui.py 的做法：自建 Flask app 只注册 admin_ui.blueprint，
admin_oauth 作为其嵌套蓝图自动继承 _admin_gate 门禁。
"""
import copy
import json
import stat
import urllib.parse

import pytest
from flask import Flask

import admin_oauth
import admin_ui
import config_store
import oauth_login
import usage_log


PORT = 51818


def _token_payload() -> dict:
    return {
        "user_id": "u1", "user_name": "tester", "domain_id": "d1",
        "refresh_token": "rt-1",
        "credentials": {
            "access_key_id": "ak-1", "secret_access_key": "sk-1",
            "security_token": "st-1", "expiration": "2036-10-18T12:00:00Z",
        },
    }


def _redirect_url(login) -> str:
    query = urllib.parse.urlencode({"ticket_id": login.ticket_id})
    return f"{oauth_login.PORTAL_HOST}/oauth/callback?{query}"


def _callback_url(login, query: str) -> str:
    return f"http://127.0.0.1:{login.port}{oauth_login.REDIRECT_PATH}?{query}"


@pytest.fixture
def cred_path(tmp_path):
    return tmp_path / "oauth-auth.json"


def _reset_admin_state() -> None:
    admin_ui._fail_count = 0
    admin_ui._fail_until = 0.0
    admin_ui._lockout_rounds = 0
    admin_ui.admin_password = ""
    config_store._data = {}
    config_store._path = ""


@pytest.fixture(autouse=True)
def _isolated(cred_path):
    """每个用例：全新会话仓库 + 临时凭据文件，避免状态与真实落盘泄漏。

    oauth_login 全局态用 deepcopy 保存：写路径当前虽均在 _cred_lock 下整体
    重绑定 _cred_state，deepcopy 仍可保证未来出现原地修改时恢复正确。
    """
    _reset_admin_state()
    original_cred = (
        oauth_login._cred_path,
        copy.deepcopy(oauth_login._cred_state),
        oauth_login._cred_needs_relogin,
        oauth_login._cred_initialized,
    )
    oauth_login.init(str(cred_path))
    admin_oauth.configure(oauth_login.OAuthSessionStore(), PORT)
    yield
    try:
        admin_oauth.configure(None, PORT)
        _reset_admin_state()
    finally:
        # 无论清理是否抛错都必须恢复 oauth_login 全局态，否则泄漏到后续测试文件
        with oauth_login._cred_lock:
            (oauth_login._cred_path, oauth_login._cred_state,
             oauth_login._cred_needs_relogin, oauth_login._cred_initialized) = original_cred


def _make_app(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({}), encoding="utf-8")
    config_store.init(str(config_file))
    admin_ui.configure(
        password="sekret",
        model_list=lambda: [],
        model_details=lambda: {},
        agent_models=[],
        refresh_models=lambda: True,
        benefit_balance=lambda: {},
        benefit_status=lambda: {},
        benefit_claim=lambda: {},
        config_path=str(config_file),
        upstream="https://example.com/api",
        host="127.0.0.1",
        port=PORT,
        cache_path=str(tmp_path / "models-cache.json"),
        usage_log_query=usage_log.query,
    )
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(admin_ui.blueprint)
    app.testing = True
    return app


@pytest.fixture
def app(tmp_path, _isolated):
    return _make_app(tmp_path)


@pytest.fixture
def client(app):
    client = app.test_client()
    client.post("/api/admin/login", json={"password": "sekret"})
    return client


def _start(client):
    resp = client.post("/api/admin/oauth/start")
    assert resp.status_code == 200
    data = resp.get_json()
    return data, admin_oauth.oauth_sessions.get(data["session_id"])


# ---------------------------------------------------------------------------
# 口令门禁（嵌套蓝图继承 admin_ui.before_request）
# ---------------------------------------------------------------------------

class TestGate:
    def test_unauthed_401(self, app):
        resp = app.test_client().get("/api/admin/oauth/status")
        assert resp.status_code == 401
        assert "error" in resp.get_json()

    def test_admin_disabled_403(self, app):
        admin_ui.admin_password = ""
        resp = app.test_client().get("/api/admin/oauth/status")
        assert resp.status_code == 403
        assert "未启用" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

class TestStart:
    def test_returns_url_without_secrets(self, client):
        data, login = _start(client)
        assert data["session_id"] == login.id
        assert data["auth_url"] == login.auth_url
        assert data["callback_hint"] == f"http://127.0.0.1:{PORT}/oauth/callback"
        body = json.dumps(data)
        assert login.verifier not in body
        assert login.secret not in body
        assert login.dpop_private_key["d"] not in body
        for forbidden in ("refresh_token", "code_verifier",
                          "secret_access_key", "security_token"):
            assert forbidden not in body

    def test_auth_url_contains_pkce_params(self, client):
        data, login = _start(client)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(data["auth_url"]).query)
        assert query["port"] == [str(PORT)]
        assert query["ticket_id"] == [login.ticket_id]
        assert query["state"] == [login.state]
        assert query["code_challenge_method"] == ["SHA-256"]
        assert len(query["code_challenge"][0]) == 43  # 32 字节 base64url 无填充

    def test_sixth_session_409(self, client):
        for _ in range(oauth_login.SESSION_MAX_PER_ADMIN):
            assert client.post("/api/admin/oauth/start").status_code == 200
        resp = client.post("/api/admin/oauth/start")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "session limit reached"


# ---------------------------------------------------------------------------
# 跨管理会话
# ---------------------------------------------------------------------------

class TestCrossSession:
    def _other_client(self, app):
        other = app.test_client()
        other.post("/api/admin/login", json={"password": "sekret"})
        return other

    def test_poll_from_other_admin_409(self, app, client):
        data, _ = _start(client)
        resp = self._other_client(app).get(
            f"/api/admin/oauth/poll?session_id={data['session_id']}")
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "session not available"

    def test_import_callback_from_other_admin_409(self, app, client):
        data, login = _start(client)
        resp = self._other_client(app).post(
            "/api/admin/oauth/import-callback",
            json={"session_id": data["session_id"],
                  "url": _callback_url(login, "code=C")})
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------

class TestPoll:
    def test_missing_session_id_400(self, client):
        resp = client.get("/api/admin/oauth/poll")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "session_id required"

    def test_unknown_session_409(self, client):
        resp = client.get("/api/admin/oauth/poll?session_id=deadbeef")
        assert resp.status_code == 409

    def test_pending_then_ok(self, client):
        data, login = _start(client)
        assert client.get(
            f"/api/admin/oauth/poll?session_id={login.id}").get_json() == {
                "status": "pending", "detail": ""}
        admin_oauth.oauth_sessions.complete(login.id)
        assert client.get(
            f"/api/admin/oauth/poll?session_id={login.id}").get_json() == {
                "status": "ok", "error": ""}

    def test_done_with_error(self, client):
        _, login = _start(client)
        admin_oauth.oauth_sessions.complete(login.id, err="OSError")
        assert client.get(
            f"/api/admin/oauth/poll?session_id={login.id}").get_json() == {
                "status": "err", "error": "OSError"}

    def test_ticket_fallback_success(self, client, monkeypatch, cred_path):
        _, login = _start(client)
        admin_oauth.oauth_sessions.enable_ticket_fallback(login.id, "portal-S1")
        seen = {}

        def _poll_ticket(ticket_id, secret):
            seen["ticket_id"] = ticket_id
            seen["secret"] = secret
            return _token_payload()

        monkeypatch.setattr(oauth_login, "poll_ticket", _poll_ticket)
        resp = client.get(f"/api/admin/oauth/poll?session_id={login.id}")
        assert resp.get_json() == {"status": "ok", "error": ""}
        assert seen == {"ticket_id": login.ticket_id, "secret": "portal-S1"}
        assert oauth_login.credentials_status()["logged_in"] is True
        assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600

    def test_ticket_fallback_transient_stays_pending(self, client, monkeypatch):
        _, login = _start(client)
        admin_oauth.oauth_sessions.enable_ticket_fallback(login.id, "portal-S1")

        def _boom(ticket_id, secret):
            raise OSError("ticket poll failed: http=502")

        monkeypatch.setattr(oauth_login, "poll_ticket", _boom)
        resp = client.get(f"/api/admin/oauth/poll?session_id={login.id}")
        assert resp.get_json() == {"status": "pending", "detail": "OSError"}

    def test_ticket_fallback_save_failure_marks_error(self, client, monkeypatch):
        _, login = _start(client)
        admin_oauth.oauth_sessions.enable_ticket_fallback(login.id, "portal-S1")
        monkeypatch.setattr(oauth_login, "poll_ticket",
                            lambda ticket_id, secret: _token_payload())

        def _boom(*args, **kwargs):
            raise OSError("credential write failed")

        monkeypatch.setattr(oauth_login, "save_credentials", _boom)
        resp = client.get(f"/api/admin/oauth/poll?session_id={login.id}")
        assert resp.get_json() == {"status": "err", "error": "OSError"}
        session = admin_oauth.oauth_sessions.get(login.id)
        assert session.done is True
        assert session.err == "OSError"


# ---------------------------------------------------------------------------
# import-callback
# ---------------------------------------------------------------------------

class TestImportCallback:
    def test_missing_params_400(self, client):
        assert client.post(
            "/api/admin/oauth/import-callback", json={}).status_code == 400
        assert client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": "x"}).status_code == 400

    @pytest.mark.parametrize("payload", [[1], "abc", 123])
    def test_non_dict_json_400(self, client, payload):
        resp = client.post("/api/admin/oauth/import-callback", json=payload)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "session_id and url required"

    @pytest.mark.parametrize("url_fmt", [
        "https://127.0.0.1:{port}/oauth/callback?code=C",        # 非 http
        "http://evil.example.com:{port}/oauth/callback?code=C",  # 非回环
        "http://127.0.0.1:9/oauth/callback?code=C",              # 端口不符
        "http://user:pw@127.0.0.1:{port}/oauth/callback?code=C", # userinfo
        "http://127.0.0.1:{port}/other?code=C",                  # path 不符
        "http://127.0.0.1:abc/oauth/callback?code=C",            # 非法端口
    ])
    def test_invalid_url_400(self, client, url_fmt):
        _, login = _start(client)
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id, "url": url_fmt.format(port=PORT)})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid callback url"

    def test_missing_code_400(self, client):
        _, login = _start(client)
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id,
                  "url": _callback_url(login, "")})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "code required"

    def test_secret_stage_enables_ticket_fallback(self, client):
        _, login = _start(client)
        redirect = _redirect_url(login)
        query = urllib.parse.urlencode({"secret": "portal-S1", "redirect": redirect})
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id, "url": _callback_url(login, query)})
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "pending", "detail": ""}
        assert admin_oauth.oauth_sessions.get(login.id).ticket_fallback is True
        assert admin_oauth.oauth_sessions.get(login.id).secret == "portal-S1"

    @pytest.mark.parametrize("redirect_fmt", [
        "http://codearts.huaweicloud.com/portal/oauth/callback?ticket_id={ticket}",
        "https://evil.example.com/portal/oauth/callback?ticket_id={ticket}",
        "https://codearts.huaweicloud.com/portal/other?ticket_id={ticket}",
        "https://codearts.huaweicloud.com/portal/oauth/callback?ticket_id=deadbeef",
    ])
    def test_invalid_redirect_400(self, client, redirect_fmt):
        _, login = _start(client)
        redirect = redirect_fmt.format(ticket=login.ticket_id)
        query = urllib.parse.urlencode({"secret": "S", "redirect": redirect})
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id, "url": _callback_url(login, query)})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid redirect parameter"
        assert admin_oauth.oauth_sessions.get(login.id).ticket_fallback is False

    def test_secret_stage_missing_ticket_id_400(self, client):
        _, login = _start(client)
        redirect = f"{oauth_login.PORTAL_HOST}/oauth/callback"
        query = urllib.parse.urlencode({"secret": "S", "redirect": redirect})
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id, "url": _callback_url(login, query)})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid redirect parameter"

    def test_secret_stage_non_ascii_ticket_id_400(self, client):
        _, login = _start(client)
        # ticket_id 经百分号解码后为非 ASCII，应 400 而非 compare_digest TypeError
        redirect = f"{oauth_login.PORTAL_HOST}/oauth/callback?ticket_id=%E4%B8%AD"
        query = urllib.parse.urlencode({"secret": "S", "redirect": redirect})
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id, "url": _callback_url(login, query)})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "invalid redirect parameter"

    def test_code_stage_success(self, client, monkeypatch, cred_path):
        _, login = _start(client)
        monkeypatch.setattr(
            oauth_login, "exchange_code",
            lambda code, verifier, port, jwk: _token_payload())
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id,
                  "url": _callback_url(login, "code=good-code")})
        assert resp.status_code == 200
        assert resp.get_json() == {"status": "ok", "error": ""}
        assert oauth_login.credentials_status()["logged_in"] is True
        assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600

    def test_code_stage_failure_502(self, client, monkeypatch):
        _, login = _start(client)

        def _boom(code, verifier, port, jwk):
            raise OSError("token request failed: http=400")

        monkeypatch.setattr(oauth_login, "exchange_code", _boom)
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id,
                  "url": _callback_url(login, "code=bad")})
        assert resp.status_code == 502
        assert resp.get_json()["error"] == "code exchange failed"
        session = admin_oauth.oauth_sessions.get(login.id)
        assert session.done is True
        assert session.err == "OSError"

    def test_code_stage_refresh_token_expired_502(self, client, monkeypatch):
        _, login = _start(client)

        def _boom(*args, **kwargs):
            raise oauth_login.RefreshTokenExpiredError()

        monkeypatch.setattr(oauth_login, "exchange_code", _boom)
        resp = client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id,
                  "url": _callback_url(login, "code=C")})
        assert resp.status_code == 502
        assert resp.get_json()["error"] == "code exchange failed"
        assert (admin_oauth.oauth_sessions.get(login.id).err
                == "RefreshTokenExpiredError")


# ---------------------------------------------------------------------------
# logout / status
# ---------------------------------------------------------------------------

class TestLogoutAndStatus:
    def test_logout_clears_credentials(self, client, monkeypatch, cred_path):
        _, login = _start(client)
        monkeypatch.setattr(
            oauth_login, "exchange_code",
            lambda code, verifier, port, jwk: _token_payload())
        client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id,
                  "url": _callback_url(login, "code=C")})
        assert client.get("/api/admin/oauth/status").get_json()["logged_in"] is True

        resp = client.post("/api/admin/oauth/logout")
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert client.get("/api/admin/oauth/status").get_json() == {
            "logged_in": False, "needs_relogin": False}
        assert not cred_path.exists()

    def test_logout_failure_502(self, client, monkeypatch):
        def _boom():
            raise OSError("denied")

        monkeypatch.setattr(oauth_login, "logout", _boom)
        resp = client.post("/api/admin/oauth/logout")
        assert resp.status_code == 502
        assert "OSError" in resp.get_json()["error"]

    def test_status_uninitialized_503(self, client, monkeypatch):
        monkeypatch.setattr(oauth_login, "_cred_initialized", False)
        resp = client.get("/api/admin/oauth/status")
        assert resp.status_code == 503
        assert resp.get_json()["error"] == "credential store not initialized"

    def test_status_never_exposes_credentials(self, client, monkeypatch):
        _, login = _start(client)
        monkeypatch.setattr(
            oauth_login, "exchange_code",
            lambda code, verifier, port, jwk: _token_payload())
        client.post(
            "/api/admin/oauth/import-callback",
            json={"session_id": login.id,
                  "url": _callback_url(login, "code=C")})
        body = json.dumps(client.get("/api/admin/oauth/status").get_json())
        for value in ("ak-1", "sk-1", "st-1", "rt-1"):
            assert value not in body
