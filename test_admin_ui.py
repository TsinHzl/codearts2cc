# -*- coding: utf-8 -*-
"""admin_ui 单元测试：鉴权 / 模型分组 / 额度 / 配置写回 / 积分用量。"""
import json
import os
import re
import stat
import time

import pytest
from flask import Flask

import admin_ui
import api_keys
import config_store
import oauth_login
import usage_log


@pytest.fixture(autouse=True)
def _reset_global_state():
    """重置 admin_ui 的进程内退避、配置与 API Key 全局，避免测试间状态泄漏。"""
    def _reset():
        admin_ui._fail_count = 0
        admin_ui._fail_until = 0.0
        admin_ui._lockout_rounds = 0
        admin_ui.admin_password = ""
        config_store._data = {}
        config_store._path = ""
        api_keys._path = ""
        api_keys._keys = []
        api_keys._initialized = False

    _reset()
    yield
    _reset()


@pytest.fixture
def app_no_pw():
    """未设置 ADMIN_PASSWORD：管理面禁用。"""
    admin_ui.configure(
        password="",
        model_list=lambda: ["openpangu-2.0-pro", "glm-5.3-flash"],
        model_details=lambda: {
            "openpangu-2.0-pro": {"id": "openpangu-2.0-pro", "name": "Pangu Pro",
                                  "channel": "agent", "context_window": 202752,
                                  "max_tokens": 131072, "supports_images": False},
            "glm-5.3-flash": {"id": "glm-5.3-flash", "name": "GLM Flash",
                              "channel": "benefit", "context_window": 131072,
                              "max_tokens": 131072, "supports_images": False},
        },
        agent_models=["openpangu-2.0-pro"],
        refresh_models=lambda: True,
        benefit_balance=lambda: {"total_quota": 100, "total_balance": 80, "used_amount": 20},
        benefit_status=lambda: {"create_time": 1792157177000},
        benefit_claim=lambda: {"create_time": 1792157177000},
        config_path="/nonexistent/config.json",
        upstream="https://example.com/api",
        host="127.0.0.1",
        port=51818,
        cache_path="/nonexistent/models-cache.json",
    )
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(admin_ui.blueprint)
    app.testing = True
    return app


@pytest.fixture
def app_with_pw(tmp_path):
    """已设置 ADMIN_PASSWORD + 临时 config.json。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "CODEARTS_AK": "AK1234567890",
        "CODEARTS_SK": "SKsecret999999",
        "CODEARTS_ANTHROPIC_MODEL_MAP": "claude-sonnet-4-5:openpangu-2.0-pro",
        "CODEARTS_CACHE_SIM": "1",
    }, ensure_ascii=False), encoding="utf-8")
    config_store.init(str(config_file))
    admin_ui.configure(
        password="sekret",
        model_list=lambda: ["openpangu-2.0-pro", "glm-5.3-flash"],
        model_details=lambda: {
            "openpangu-2.0-pro": {"id": "openpangu-2.0-pro", "name": "Pangu Pro",
                                  "channel": "agent", "context_window": 202752,
                                  "max_tokens": 131072, "supports_images": False},
            "glm-5.3-flash": {"id": "glm-5.3-flash", "name": "GLM Flash",
                              "channel": "benefit", "context_window": 131072,
                              "max_tokens": 131072, "supports_images": False},
        },
        agent_models=["openpangu-2.0-pro"],
        refresh_models=lambda: True,
        benefit_balance=lambda: {"total_quota": 100, "total_balance": 80, "used_amount": 20},
        benefit_status=lambda: {"create_time": 1792157177000},
        benefit_claim=lambda: {"create_time": 1792157177000},
        config_path=str(config_file),
        upstream="https://example.com/api",
        host="0.0.0.0",
        port=51818,
        cache_path=str(tmp_path / "models-cache.json"),
        usage_log_query=usage_log.query,
    )
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(admin_ui.blueprint)
    app.testing = True
    return app


# ---------------------------------------------------------------------------
# T1 鉴权
# ---------------------------------------------------------------------------

class TestAuth:
    def test_disabled_page_403(self, app_no_pw):
        client = app_no_pw.test_client()
        resp = client.get("/admin")
        assert resp.status_code == 403

    def test_disabled_api_403(self, app_no_pw):
        client = app_no_pw.test_client()
        resp = client.get("/api/admin/models")
        assert resp.status_code == 403
        data = resp.get_json()
        assert "未启用" in data["error"]
        assert "openpangu" not in data["error"]  # 不泄漏模型数据

    def test_unauthed_api_401(self, app_with_pw):
        client = app_with_pw.test_client()
        resp = client.get("/api/admin/models")
        assert resp.status_code == 401

    def test_login_success(self, app_with_pw):
        client = app_with_pw.test_client()
        resp = client.post("/api/admin/login", json={"password": "sekret"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        # 登录后可访问受保护接口
        assert client.get("/api/admin/models").status_code == 200

    def test_login_wrong_password(self, app_with_pw):
        client = app_with_pw.test_client()
        resp = client.post("/api/admin/login", json={"password": "wrong"})
        assert resp.status_code == 401

    def test_login_backoff_throttle(self, app_with_pw):
        client = app_with_pw.test_client()
        # 连续失败 5 次触发退避
        for _ in range(5):
            client.post("/api/admin/login", json={"password": "wrong"})
        resp = client.post("/api/admin/login", json={"password": "sekret"})
        assert resp.status_code == 429

    def test_login_success_resets_backoff(self, app_with_pw):
        """CR 修复回归：成功登录重置退避轮数，再失败 5 次才触发新一轮退避。"""
        client = app_with_pw.test_client()
        for _ in range(5):
            client.post("/api/admin/login", json={"password": "wrong"})
        assert client.post("/api/admin/login", json={"password": "wrong"}).status_code == 429
        admin_ui._fail_until = 0.0  # 模拟退避到期（不等待真实 5 秒）
        assert client.post("/api/admin/login", json={"password": "sekret"}).status_code == 200
        # 成功登录后 _lockout_rounds 归零：再次连续失败 5 次，退避仍是首轮的短退避
        for _ in range(5):
            client.post("/api/admin/login", json={"password": "wrong"})
        assert admin_ui._lockout_rounds == 1  # 若未重置则为 2（第二轮指数）
        assert client.post("/api/admin/login", json={"password": "sekret"}).status_code == 429

    def test_shell_page_served_when_unauthed(self, app_with_pw):
        """已启用 + 未登录：GET /admin 重定向后壳页面放行（React 壳可加载），数据接口仍 401。"""
        client = app_with_pw.test_client()
        resp = client.get("/admin/")
        assert resp.status_code == 200
        assert b'id="root"' in resp.data
        assert client.get("/api/admin/models").status_code == 401


# ---------------------------------------------------------------------------
# T2 模型聚合
# ---------------------------------------------------------------------------

class TestModels:
    def test_grouping(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/models")
        assert resp.status_code == 200
        data = resp.get_json()
        assert [m["id"] for m in data["agent"]] == ["openpangu-2.0-pro"]
        assert [m["id"] for m in data["benefit"]] == ["glm-5.3-flash"]
        assert data["agent"][0]["context_window"] == 202752
        assert data["benefit"][0]["supports_images"] is False


# ---------------------------------------------------------------------------
# T3 额度 / 签到
# ---------------------------------------------------------------------------

class TestBenefit:
    @pytest.fixture(autouse=True)
    def _logged_in_oauth(self, tmp_path):
        """benefit 端点未登录会 503：注入假已登录凭据再测原行为。"""
        oauth_login.init(str(tmp_path / "oauth-auth.json"))
        oauth_login.save_credentials(
            {
                "user_id": "u1", "user_name": "t", "domain_id": "d1",
                "refresh_token": "rt",
                "credentials": {
                    "access_key_id": "ak", "secret_access_key": "sk",
                    "security_token": "st",
                    "expiration": "2099-01-01T00:00:00Z",
                },
            },
            code_verifier="v",
            dpop_private_jwk=oauth_login.new_dpop_private_jwk(),
        )
        yield
        oauth_login.init(str(tmp_path / "unused.json"))

    def test_balance(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/benefit").get_json()
        assert data["total_quota"] == 100
        assert data["total_balance"] == 80

    def test_claim(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.post("/api/admin/benefit/claim").get_json()
        assert data["ok"] is True

    def test_claim_failure(self, app_with_pw, monkeypatch):
        def _fail():
            raise RuntimeError("上游失败")
        monkeypatch.setattr(admin_ui, "benefit_claim_fn", _fail)
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.post("/api/admin/benefit/claim")
        assert resp.status_code == 502
        assert "签到失败" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# T4 配置写回
# ---------------------------------------------------------------------------

class TestConfig:
    def test_get_config_masks_secrets(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/config").get_json()
        assert data["secrets"]["CODEARTS_AK"].startswith("AK")
        assert "AK1234567890" not in data["secrets"]["CODEARTS_AK"]
        assert data["secrets"]["CODEARTS_SK"].startswith("SK")
        assert data["secrets"]["CODEARTS_SK"].endswith("99")  # 尾部 2 字符保留
        assert "secret" not in data["secrets"]["CODEARTS_SK"]  # 中部脱敏

    def test_edit_map_writes_config(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_ANTHROPIC_MODEL_MAP": "new-alias:GLM-5.2"}
        })
        assert resp.status_code == 200
        # 文件更新且 AK/SK 键值不变
        with open(admin_ui.config_path, encoding="utf-8") as f:
            data = json.loads(f.read())
        assert data["CODEARTS_AK"] == "AK1234567890"
        assert data["CODEARTS_SK"] == "SKsecret999999"
        assert data["CODEARTS_ANTHROPIC_MODEL_MAP"] == "new-alias:GLM-5.2"

    def test_edit_unknown_key_rejected(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={"editable": {"CODEARTS_AK": "x"}})
        assert resp.status_code == 400

    def test_edit_multiline_value_rejected(self, app_with_pw):
        """CR 修复回归：值含换行（可伪造配置键行）必须被拒。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_CACHE_SIM": "0\nADMIN_PASSWORD=attacker"}
        })
        assert resp.status_code == 400
        # 文件未被改动
        with open(admin_ui.config_path, encoding="utf-8") as f:
            data = json.loads(f.read())
        assert "ADMIN_PASSWORD" not in data

    def test_edit_non_string_value_rejected(self, app_with_pw):
        """CR 修复回归：非字符串值（bool/dict repr 污染配置文件）必须被拒。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_CACHE_SIM": True}
        })
        assert resp.status_code == 400

    def test_thinking_as_text_default_echoed(self, app_with_pw):
        """键缺失时 GET 回显默认值 1（默认开启）。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/config").get_json()
        assert data["editable"]["CODEARTS_THINKING_AS_TEXT"] == "1"

    def test_thinking_as_text_roundtrip(self, app_with_pw):
        """写入 0 后配置落盘并可读回。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_THINKING_AS_TEXT": "0"}
        })
        assert resp.status_code == 200
        with open(admin_ui.config_path, encoding="utf-8") as f:
            assert json.loads(f.read())["CODEARTS_THINKING_AS_TEXT"] == "0"
        data = client.get("/api/admin/config").get_json()
        assert data["editable"]["CODEARTS_THINKING_AS_TEXT"] == "0"

    def test_require_api_key_default_echoed(self, app_with_pw):
        """键缺失时 GET 回显默认值 1（默认强制鉴权）。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/config").get_json()
        assert data["editable"]["CODEARTS_REQUIRE_API_KEY"] == "1"

    def test_require_api_key_roundtrip(self, app_with_pw):
        """写入 0 后配置落盘并可读回（热生效，无需重启）。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_REQUIRE_API_KEY": "0"}
        })
        assert resp.status_code == 200
        with open(admin_ui.config_path, encoding="utf-8") as f:
            assert json.loads(f.read())["CODEARTS_REQUIRE_API_KEY"] == "0"
        data = client.get("/api/admin/config").get_json()
        assert data["editable"]["CODEARTS_REQUIRE_API_KEY"] == "0"

    @pytest.mark.parametrize("value", ["2", "true", "", "1 ", "yes"])
    def test_require_api_key_rejects_other_values(self, app_with_pw, value):
        """只接受 0 / 1；非法值 400 且不写回 config.json。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_REQUIRE_API_KEY": value}
        })
        assert resp.status_code == 400
        with open(admin_ui.config_path, encoding="utf-8") as f:
            assert "CODEARTS_REQUIRE_API_KEY" not in json.loads(f.read())


class TestSecretsWriteback:
    """secrets 回写通路：掩码展示 + PUT 新值生效 + 掩码/纯 * 不覆盖。"""

    def test_get_config_masks_snap_session(self, app_with_pw):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "SEcretSessionValue12345"})
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/config").get_json()
        masked = data["secrets"]["CODEARTS_SNAP_SESSION"]
        assert masked.startswith("SE")
        assert masked.endswith("45")
        assert "ecretSession" not in masked  # 中部脱敏

    def test_put_new_session_value_applies(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config/secrets", json={
            "secrets": {"CODEARTS_SNAP_SESSION": "brand-new-session-value"}
        })
        assert resp.status_code == 200
        assert config_store.get("CODEARTS_SNAP_SESSION") == "brand-new-session-value"
        # 文件真实落盘且 AK/SK 不受影响
        with open(admin_ui.config_path, encoding="utf-8") as f:
            data = json.loads(f.read())
        assert data["CODEARTS_SNAP_SESSION"] == "brand-new-session-value"
        assert data["CODEARTS_AK"] == "AK1234567890"
        assert data["CODEARTS_SK"] == "SKsecret999999"

    def test_put_masked_value_skipped(self, app_with_pw):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "real-session-value-999"})
        masked = admin_ui._mask("real-session-value-999")
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config/secrets", json={
            "secrets": {"CODEARTS_SNAP_SESSION": masked}
        })
        assert resp.status_code == 200
        assert config_store.get("CODEARTS_SNAP_SESSION") == "real-session-value-999"

    def test_put_all_stars_skipped(self, app_with_pw):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "real-session-value-999"})
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config/secrets", json={
            "secrets": {"CODEARTS_SNAP_SESSION": "****"}
        })
        assert resp.status_code == 200
        assert config_store.get("CODEARTS_SNAP_SESSION") == "real-session-value-999"

    def test_put_non_secret_key_rejected(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config/secrets", json={
            "secrets": {"CODEARTS_CACHE_SIM": "0"}
        })
        assert resp.status_code == 400

    def test_put_ak_key_rejected(self, app_with_pw):
        """AK/SK 写入需重启才生效（server 启动时固化），维持只读。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config/secrets", json={
            "secrets": {"CODEARTS_AK": "NEWAK"}
        })
        assert resp.status_code == 400
        assert config_store.get("CODEARTS_AK") == "AK1234567890"


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------

class TestOverview:
    def test_overview_public_exposed(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/overview").get_json()
        assert data["public_exposed"] is True
        assert data["model_count"] == 2
        assert data["agent_count"] == 1
        assert data["benefit_count"] == 1

# ---------------------------------------------------------------------------
# 积分用量（/api/admin/credit）
# ---------------------------------------------------------------------------

def _credit_ok_payload() -> str:
    return json.dumps({"all_credit_package": {
        "package_credit_amount": 7500.0,
        "package_credit_used": 972.77,
        "package_credit_remain": 6527.23,
    }}, ensure_ascii=False)


class TestCredit:
    def test_credit_unauthed_401(self, app_with_pw):
        client = app_with_pw.test_client()
        assert client.get("/api/admin/credit").status_code == 401

    def test_credit_session_missing_400(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 400
        assert "CODEARTS_SNAP_SESSION" in resp.get_json()["error"]

    def test_credit_success_normalized(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "fake-session-value"})
        captured = {}

        class _FakeResp:
            status_code = 200
            text = _credit_ok_payload()

        def _fake_get(url, **kwargs):
            captured["url"] = url
            captured["cookies"] = kwargs.get("cookies")
            return _FakeResp()

        monkeypatch.setattr(admin_ui.requests, "get", _fake_get)
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        data = client.get("/api/admin/credit").get_json()
        assert data == {"amount": 7500.0, "used": 972.77, "remain": 6527.23}
        assert captured["url"] == admin_ui._CREDIT_URL
        assert captured["cookies"] == {"devclouddevuibjJ_SESSION_ID": "fake-session-value"}
        # config.json 权限保持 0600（set_many 原子写回兜底）
        assert stat.S_IMODE(os.stat(admin_ui.config_path).st_mode) == 0o600

    def test_credit_empty_body_session_expired(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "expired-session"})

        class _FakeResp:
            status_code = 200
            text = ""

        monkeypatch.setattr(admin_ui.requests, "get", lambda url, **kw: _FakeResp())
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        assert "积分会话已过期" in resp.get_json()["error"]

    def test_credit_non_json_body_session_expired(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "expired-session"})

        class _FakeResp:
            status_code = 200
            text = "<html>redirect to login</html>"

        monkeypatch.setattr(admin_ui.requests, "get", lambda url, **kw: _FakeResp())
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        assert "积分会话已过期" in resp.get_json()["error"]

    def test_credit_missing_fields_structure_error(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "some-session"})

        class _FakeResp:
            status_code = 200
            text = json.dumps({"all_credit_package": {"package_credit_amount": 100}})

        monkeypatch.setattr(admin_ui.requests, "get", lambda url, **kw: _FakeResp())
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        assert "响应结构异常" in resp.get_json()["error"]

    def test_credit_missing_package_structure_error(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "some-session"})

        class _FakeResp:
            status_code = 200
            text = json.dumps({"unexpected": True})

        monkeypatch.setattr(admin_ui.requests, "get", lambda url, **kw: _FakeResp())
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        assert "响应结构异常" in resp.get_json()["error"]

    def test_credit_upstream_non200(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "some-session"})

        class _FakeResp:
            status_code = 503
            text = ""

        monkeypatch.setattr(admin_ui.requests, "get", lambda url, **kw: _FakeResp())
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        assert "积分接口请求失败" in resp.get_json()["error"]

    def test_credit_upstream_timeout_no_cookie_leak(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "secret-session-value"})

        def _timeout(url, **kwargs):
            raise admin_ui.requests.Timeout("connect timeout for "
                                            "cookie=devclouddevuibjJ_SESSION_ID=secret-session-value")

        monkeypatch.setattr(admin_ui.requests, "get", _timeout)
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        message = resp.get_json()["error"]
        assert "积分接口请求失败" in message
        assert "secret-session-value" not in message  # Cookie 值不落错误信息

    def test_credit_connection_error_no_leak(self, app_with_pw, monkeypatch):
        config_store.set_many({"CODEARTS_SNAP_SESSION": "secret-session-value"})

        def _conn_err(url, **kwargs):
            raise admin_ui.requests.ConnectionError(
                "connection refused for cookie=devclouddevuibjJ_SESSION_ID=secret-session-value")

        monkeypatch.setattr(admin_ui.requests, "get", _conn_err)
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.get("/api/admin/credit")
        assert resp.status_code == 502
        message = resp.get_json()["error"]
        assert "积分接口请求失败" in message
        assert "secret-session-value" not in message


# ---------------------------------------------------------------------------
# 管理界面壳（React 预构建产物，Vite 输出在 assets/ 子目录带 hash 文件名）
# ---------------------------------------------------------------------------

class TestUsageTab:
    def test_admin_shell_serves_react_bundle(self, app_with_pw):
        """/admin 重定向到 /admin/ 并服务 admin-ui/dist/index.html（React 壳），静态资源可获取。"""
        client = app_with_pw.test_client()
        resp = client.get("/admin")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/admin/")
        resp = client.get("/admin/")
        assert resp.status_code == 200
        assert resp.content_type.startswith("text/html")
        assert resp.headers["Cache-Control"] == "no-store"
        html = resp.data.decode("utf-8")
        assert 'id="root"' in html
        # Vite 产物：index.html 引用 /admin/assets/ 下带 hash 的 bundle
        js_paths = re.findall(r'src="(/admin/assets/[^"]+\.js)"', html)
        css_paths = re.findall(r'href="(/admin/assets/[^"]+\.css)"', html)
        assert js_paths, "index.html 未引用任何 /admin/assets/*.js"
        assert css_paths, "index.html 未引用任何 /admin/assets/*.css"
        # bundle 为真实构建产物（含 React 挂载代码），非占位；assets/ 长 immutable 缓存
        js = client.get(js_paths[0])
        assert js.status_code == 200
        assert "javascript" in js.content_type
        assert len(js.data) > 10000
        assert "immutable" in js.headers["Cache-Control"]
        css = client.get(css_paths[0])
        assert css.status_code == 200
        assert "css" in css.content_type
        # dist 外路径与不存在文件一律 404
        assert client.get("/admin/../server.py").status_code == 404
        assert client.get("/admin/nope.js").status_code == 404
        assert client.get("/admin/assets/nope.js").status_code == 404


class TestAdminGateDisabled:
    def test_shell_and_assets_403_without_password(self, app_no_pw):
        """未配置 ADMIN_PASSWORD 时壳与静态资源均被 before_request 拦截 403。"""
        client = app_no_pw.test_client()
        assert client.get("/admin").status_code == 403
        assert client.get("/admin/").status_code == 403
        assert client.get("/admin/assets/nope.js").status_code == 403
        assert client.get("/admin/assets/nope.css").status_code == 403


# ---------------------------------------------------------------------------
# 使用日志
# ---------------------------------------------------------------------------

def _usage_entry(**overrides):
    """构造一条 usage_log 记录（默认带 cache_creation_tokens）。"""
    entry = {
        "ts": "2026-10-17T10:00:00", "endpoint": "anthropic", "ip": "127.0.0.1",
        "model": "glm-5.3-flash", "stream": True, "effort": None,
        "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 50,
        "cache_creation_tokens": 0, "first_token_ms": 300, "duration_ms": 1500,
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def usage_log_isolated(tmp_path, monkeypatch):
    """重置 usage_log 模块状态并在临时目录初始化。"""
    monkeypatch.setattr(usage_log, "_initialized", False)
    usage_log.init(str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(usage_log, "_initialized", False)


def _login(client):
    """登录 admin 会话。"""
    client.post("/api/admin/login", json={"password": "sekret"})
    return client


class TestUsage:
    def test_usage_unauthed_401(self, app_with_pw):
        client = app_with_pw.test_client()
        assert client.get("/api/admin/usage").status_code == 401

    def test_usage_query(self, app_with_pw, monkeypatch, tmp_path):
        """登录后返回统计与明细（走注入的 usage_log.query）。"""
        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(tmp_path))
        usage_log.record({
            "ts": "2026-10-17T10:00:00", "endpoint": "anthropic", "ip": "127.0.0.1",
            "model": "glm-5.3-flash", "stream": True, "effort": None,
            "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 50,
            "first_token_ms": 300, "duration_ms": 1500,
        })
        try:
            client = app_with_pw.test_client()
            client.post("/api/admin/login", json={"password": "sekret"})
            data = client.get("/api/admin/usage").get_json()
            assert data["summary"]["requests"] == 1
            assert data["summary"]["input_tokens"] == 100
            assert data["page"]["total"] == 1
            assert data["entries"][0]["endpoint"] == "anthropic"
            # 分页参数生效
            data2 = client.get("/api/admin/usage?page=1&limit=200").get_json()
            assert data2["page"]["limit"] == 200
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)

    def test_usage_date_range_filter(self, app_with_pw, usage_log_isolated):
        """start/end 按日期过滤且含首尾。"""
        for day in (10, 11, 12, 13):
            usage_log.record(_usage_entry(ts=f"2026-10-{day:02d}T09:00:00"))
        client = _login(app_with_pw.test_client())
        data = client.get("/api/admin/usage", query_string={
            "start": "2026-10-11", "end": "2026-10-12",
        }).get_json()
        assert data["page"]["total"] == 2
        assert data["summary"]["requests"] == 2
        assert [entry["ts"][:10] for entry in data["entries"]] == [
            "2026-10-12", "2026-10-11",
        ]
        assert data["start"] == "2026-10-11"
        assert data["end"] == "2026-10-12"

    @pytest.mark.parametrize("value", [
        "2026-13-01", "2026-10-32", "2026/10/01", "abc", "2026-1-1",
    ])
    def test_usage_invalid_date_rejected(self, app_with_pw, value):
        """非法日期格式或真实日期返回 400。"""
        client = _login(app_with_pw.test_client())
        response = client.get("/api/admin/usage", query_string={"start": value})
        assert response.status_code == 400

    def test_usage_start_after_end_rejected(self, app_with_pw):
        """start 晚于 end 返回 400。"""
        client = _login(app_with_pw.test_client())
        response = client.get("/api/admin/usage", query_string={
            "start": "2026-10-13", "end": "2026-10-11",
        })
        assert response.status_code == 400

    def test_usage_daily_aggregation_prices(self, app_with_pw, usage_log_isolated):
        """daily 聚合按模型逐条计价后合并进日桶。"""
        config_store.set_many({"CODEARTS_PRICING": json.dumps({
            "glm-5.3-flash": {"input": 1.0, "output": 2.0,
                              "cache_read": 0.5, "cache_creation": 0.0},
        })})
        usage_log.record(_usage_entry())
        usage_log.record(_usage_entry(ts="2026-10-17T11:00:00"))
        client = _login(app_with_pw.test_client())
        data = client.get("/api/admin/usage?daily=true").get_json()
        assert data["daily"] is not None
        assert len(data["daily"]) == 1
        row = data["daily"][0]
        assert row["date"] == "2026-10-17"
        assert row["requests"] == 2
        assert row["cost"] == pytest.approx(0.00033)
        assert row["credits"] == pytest.approx(0.00033)
        assert row["saved"] == pytest.approx(0.00005)
        assert row["unpriced_requests"] == 0
        assert data["page_totals"]["priced_requests"] == 2
        assert client.get("/api/admin/usage").get_json()["daily"] is None

    def test_usage_account_injection(self, app_with_pw, usage_log_isolated):
        """账号名来自配置，缺失或空白回退「主账号」。"""
        usage_log.record(_usage_entry())
        client = _login(app_with_pw.test_client())
        assert client.get("/api/admin/usage").get_json()["account"] == "主账号"
        config_store.set_many({"CODEARTS_ACCOUNT_NAME": "测试账号"})
        data = client.get("/api/admin/usage").get_json()
        assert data["account"] == "测试账号"
        assert data["entries"][0]["account"] == "测试账号"
        config_store.set_many({"CODEARTS_ACCOUNT_NAME": "   "})
        assert client.get("/api/admin/usage").get_json()["account"] == "主账号"

    def test_usage_daily_prices_all_pages(self, app_with_pw, usage_log_isolated):
        """daily 计价基于 (日期, 模型) 聚合桶，超过单页上限也完整计入。"""
        config_store.set_many({"CODEARTS_PRICING": json.dumps({
            "glm-5.3-flash": {"input": 1.0, "output": 2.0,
                              "cache_read": 0.5, "cache_creation": 0.0},
        })})
        for index in range(200):
            usage_log.record(_usage_entry(ts=f"2026-10-16T10:{index // 60:02d}:{index % 60:02d}"))
        for index in range(50):
            usage_log.record(_usage_entry(ts=f"2026-10-17T10:{index // 60:02d}:{index % 60:02d}"))
        client = _login(app_with_pw.test_client())
        data = client.get("/api/admin/usage?daily=true").get_json()
        rows = {row["date"]: row for row in data["daily"]}
        assert rows["2026-10-16"]["requests"] == 200
        assert rows["2026-10-17"]["requests"] == 50
        assert rows["2026-10-16"]["cost"] == pytest.approx(0.033)
        assert rows["2026-10-17"]["cost"] == pytest.approx(0.00825)
        assert rows["2026-10-16"]["saved"] == pytest.approx(0.005)
        assert sum(row["requests"] for row in data["daily"]) == 250
        assert data["page_totals"]["priced_requests"] == 100  # 默认每页 100 条

    def test_usage_daily_dirty_tokens_price_as_zero(self, app_with_pw, usage_log_isolated):
        """桶内脏 token 折算为 0 后按 0 正常计价，不计入未计价条数。"""
        config_store.set_many({"CODEARTS_PRICING": json.dumps({
            "glm-5.3-flash": {"input": 1.0, "output": 2.0,
                              "cache_read": 0.5, "cache_creation": 0.0},
        })})
        usage_log.record(_usage_entry(input_tokens="abc", output_tokens="y",
                                      cache_read_tokens="x", cache_creation_tokens="z"))
        client = _login(app_with_pw.test_client())
        row = client.get("/api/admin/usage?daily=true").get_json()["daily"][0]
        assert (row["cost"], row["credits"], row["saved"]) == (0.0, 0.0, 0.0)
        assert row["unpriced_requests"] == 0

    def test_usage_daily_queries_log_once(self, app_with_pw, usage_log_isolated,
                                          monkeypatch):
        """daily 计价复用同一次 usage_log 查询，不重复翻页。"""
        usage_log.record(_usage_entry())
        calls = []
        original = admin_ui.usage_log_query_fn
        monkeypatch.setattr(admin_ui, "usage_log_query_fn",
                            lambda **kwargs: calls.append(1) or original(**kwargs))
        client = _login(app_with_pw.test_client())
        assert client.get("/api/admin/usage?daily=true").get_json()["daily"] is not None
        assert len(calls) == 1

    def test_usage_limit_defaults_and_clamps(self, app_with_pw, usage_log_isolated):
        """limit 缺省 100，非法回退 100，越界钳制到 [1, 200]。"""
        usage_log.record(_usage_entry())
        client = _login(app_with_pw.test_client())
        assert client.get("/api/admin/usage").get_json()["page"]["limit"] == 100
        assert client.get("/api/admin/usage?limit=abc").get_json()["page"]["limit"] == 100
        assert client.get("/api/admin/usage?limit=999").get_json()["page"]["limit"] == 200
        assert client.get("/api/admin/usage?limit=0").get_json()["page"]["limit"] == 1

    @pytest.mark.parametrize("raw", [
        "not json",
        "[]",
        '{"glm-5.3-flash": []}',
        '{"glm-5.3-flash": {"input": 1}}',
        '{"glm-5.3-flash": {"input": -1, "output": 1}}',
        '{"glm-5.3-flash": {"input": true, "output": 1}}',
        '{"glm-5.3-flash": {"input": NaN, "output": 1}}',
    ])
    def test_usage_invalid_pricing_rejected(self, app_with_pw, raw):
        """非法单价表返回 400 且配置不变。"""
        client = _login(app_with_pw.test_client())
        before = config_store.as_dict()
        response = client.put("/api/admin/config",
                              json={"editable": {"CODEARTS_PRICING": raw}})
        assert response.status_code == 400
        assert config_store.as_dict() == before

    @pytest.mark.parametrize("raw", ["abc", "-1", "nan", "inf", "1e999"])
    def test_usage_invalid_credits_rate_rejected(self, app_with_pw, raw):
        """非法 Credits 系数返回 400。"""
        client = _login(app_with_pw.test_client())
        response = client.put("/api/admin/config",
                              json={"editable": {"CODEARTS_CREDITS_RATE": raw}})
        assert response.status_code == 400

    def test_usage_config_saved_takes_effect(self, app_with_pw, usage_log_isolated):
        """保存单价表与系数后下一次查询立即生效。"""
        usage_log.record(_usage_entry())
        client = _login(app_with_pw.test_client())
        response = client.put("/api/admin/config", json={"editable": {
            "CODEARTS_PRICING": ('{"glm-5.3-flash": '
                                 '{"input": 1.0, "output": 2.0, "cache_read": 0.5}}'),
            "CODEARTS_CREDITS_RATE": "2.0",
        }})
        assert response.status_code == 200
        entry = client.get("/api/admin/usage").get_json()["entries"][0]
        assert entry["cost"] == pytest.approx(0.000165)
        assert entry["credits"] == pytest.approx(0.00033)
        assert entry["saved"] == pytest.approx(0.000025)

    def test_usage_dirty_entry_not_500(self, app_with_pw, usage_log_isolated):
        """脏 token 记录按未计价处理，查询不返回 500。"""
        config_store.set_many({"CODEARTS_PRICING": json.dumps({
            "glm-5.3-flash": {"input": 1.0, "output": 2.0},
        })})
        usage_log.record(_usage_entry(input_tokens="abc"))
        client = _login(app_with_pw.test_client())
        response = client.get("/api/admin/usage")
        assert response.status_code == 200
        data = response.get_json()
        assert data["entries"][0]["cost"] is None
        assert data["page_totals"]["priced_requests"] == 0
        assert data["page_totals"]["cost"] is None
        assert data["page_totals"]["credits"] is None
        assert data["page_totals"]["saved"] is None

    def test_usage_non_string_model_aggregates_as_unknown(self, usage_log_isolated, monkeypatch):
        """非字符串 model 按 unknown 聚合：记录、回放与区间查询均不抛错。"""
        usage_log.record(_usage_entry(model={"dirty": True}))
        # record 期聚合（_apply_to_summary）同样必须容忍非法类型
        assert usage_log.query()["by_model"]["unknown"]["requests"] == 1
        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(usage_log_isolated))
        result = usage_log.query()
        assert result["summary"]["requests"] == 1
        assert result["by_model"]["unknown"]["requests"] == 1
        assert result["entries"][0]["model"] == {"dirty": True}
        ranged = usage_log.query(start="2026-10-17", end="2026-10-17")
        assert ranged["summary"]["requests"] == 1
        assert ranged["by_model"]["unknown"]["requests"] == 1


class TestServerUsageRecording:
    """OpenAI 端点记录：cache_creation_tokens 显式写 0。"""

    def test_openai_record_writes_zero_cache_creation(self, monkeypatch):
        """直调 server._record_usage，不读取上游 cache 字段。"""
        # import server 会执行模块级 config_store.init、api_keys.init 与 AK/SK 校验，
        # 先注入假配置，避免测试进程读取仓库真实的 api-keys.json
        monkeypatch.setattr(config_store, "init", lambda path: None)
        monkeypatch.setattr(api_keys, "init", lambda path: None)
        monkeypatch.setattr(
            config_store, "get",
            lambda key, default="": {
                "CODEARTS_AK": "test-ak",
                "CODEARTS_SK": "test-sk",
                "CODEARTS_PROXY_PORT": "51818",
            }.get(key, default),
        )
        import server
        captured = {}
        monkeypatch.setattr(usage_log, "record", lambda entry: captured.update(entry))
        server._record_usage(
            model="glm-5.3-flash", stream=False, client_ip="127.0.0.1",
            effort=None, usage={"prompt_tokens": 12, "completion_tokens": 3},
            first_token_ms=None, started_at=time.monotonic(),
        )
        assert captured["cache_creation_tokens"] == 0
        assert captured["cache_read_tokens"] == 0
        assert captured["input_tokens"] == 12
        assert captured["output_tokens"] == 3


