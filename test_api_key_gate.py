# -*- coding: utf-8 -*-
"""server.py 的 /v1/* API Key 鉴权门禁测试。

覆盖 spec「请求鉴权门禁」的 11 条 Scenario。测试骨架沿用 test_admin_ui.py
的做法：import server 前先注入假配置，避免模块级 AK/SK 校验 SystemExit、
真实 config.json / api-keys.json 读写与真实上游请求。
"""
import pytest

import api_keys
import config_store


@pytest.fixture(scope="module")
def server_module():
    """安全地 import server，并把 app 标记为 testing。

    若 test_admin_ui.py 已先 import 过 server（模块缓存），下面的 patch 不会
    再影响其模块级全局；因此 ADMIN_PASSWORD 等快照值在每个用例中显式覆盖，
    不依赖测试文件的执行顺序。
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(config_store, "init", lambda path: None)
    mp.setattr(config_store, "get", lambda key, default="": {
        "CODEARTS_AK": "test-ak",
        "CODEARTS_SK": "test-sk",
        "CODEARTS_PROXY_PORT": "51818",
        # 非空 ADMIN_SECRET_KEY 让 _session_salt() 短路，避免写仓库真实 .admin_salt；
        # 非空 ADMIN_PASSWORD 让 admin_ui 在 import 时完成口令配置（管理面启用）。
        "ADMIN_PASSWORD": "test-pw",
        "ADMIN_SECRET_KEY": "test-secret",
    }.get(key, default))
    mp.setattr(api_keys, "init", lambda path: None)
    try:
        import server as mod
    finally:
        mp.undo()
    mod.app.testing = True
    return mod


@pytest.fixture
def config_state(monkeypatch):
    """可控的 config_store 读取：默认强制鉴权开启。"""
    state = {"CODEARTS_REQUIRE_API_KEY": "1"}

    def fake_get(key, default=""):
        return state.get(key, default)

    monkeypatch.setattr(config_store, "get", fake_get)
    return state


@pytest.fixture
def client(server_module, monkeypatch, tmp_path, config_state):
    """每个用例独立的 api-keys.json，管理面启用，模型刷新与上游调用被屏蔽。"""
    api_keys.init(str(tmp_path / "api-keys.json"))
    monkeypatch.setattr(server_module, "ADMIN_PASSWORD", "test-pw")
    monkeypatch.setattr(server_module, "_refresh_models_if_needed", lambda: None)
    monkeypatch.setattr(server_module, "SUPPORTED_MODELS", ["glm-5.3-flash"])
    monkeypatch.setattr(server_module, "MODEL_DETAILS", {})
    return server_module.app.test_client()


class TestRequestGate:
    def test_valid_x_api_key_passes(self, client):
        entry = api_keys.create_key("测试")
        resp = client.get("/v1/models", headers={"x-api-key": entry["key"]})
        assert resp.status_code == 200

    def test_bearer_passes(self, client):
        entry = api_keys.create_key("测试")
        resp = client.get(
            "/v1/models",
            headers={"Authorization": f"Bearer {entry['key']}"},
        )
        assert resp.status_code == 200

    def test_x_api_key_takes_priority_over_bearer(self, client):
        entry = api_keys.create_key("测试")
        resp = client.get("/v1/models", headers={
            "x-api-key": "sk-无效值",
            "Authorization": f"Bearer {entry['key']}",
        })
        assert resp.status_code == 401

    def test_missing_key_with_empty_list_mentions_no_key_created(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 401
        message = resp.get_json()["error"]["message"]
        assert "尚未创建任何 API Key" in message

    def test_missing_key_with_non_empty_list_points_to_header(self, client):
        api_keys.create_key("测试")
        resp = client.get("/v1/models")
        assert resp.status_code == 401
        message = resp.get_json()["error"]["message"]
        assert "x-api-key" in message

    def test_options_preflight_is_exempt_and_allows_x_api_key(self, client):
        resp = client.options("/v1/chat/completions", headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key",
        })
        assert resp.status_code == 200
        assert "x-api-key" in resp.headers["Access-Control-Allow-Headers"]

    def test_invalid_key_returns_401_without_plaintext(self, client):
        entry = api_keys.create_key("测试")
        resp = client.get("/v1/models", headers={"x-api-key": "sk-完全错误"})
        assert resp.status_code == 401
        body = resp.get_data(as_text=True)
        assert entry["key"] not in body
        assert "sk-完全错误" not in body

    def test_disabled_key_returns_403(self, client):
        entry = api_keys.create_key("测试")
        api_keys.update_key(entry["id"], enabled=False)
        resp = client.get("/v1/models", headers={"x-api-key": entry["key"]})
        assert resp.status_code == 403

    def test_switch_off_passes_without_key(self, client, config_state):
        config_state["CODEARTS_REQUIRE_API_KEY"] = "0"
        assert client.get("/v1/models").status_code == 200

    def test_non_ascii_key_returns_401_not_500(self, client):
        api_keys.create_key("测试")
        resp = client.get("/v1/models", headers={"x-api-key": "sk-\xff\xfe"})
        assert resp.status_code == 401

    def test_very_long_key_returns_401_not_500(self, client):
        api_keys.create_key("测试")
        resp = client.get(
            "/v1/models",
            headers={"x-api-key": "sk-" + "a" * 8000},
        )
        assert resp.status_code == 401

    def test_admin_paths_are_exempt(self, client):
        # 管理面走自己的口令会话鉴权，不得被 API Key 门禁改写
        resp = client.get("/admin")
        assert resp.status_code != 401
        resp = client.get("/api/admin/config")
        assert resp.status_code in (401, 403)
        assert "API Key" not in resp.get_data(as_text=True)

    def test_management_disabled_passes_without_key(
        self, client, monkeypatch, server_module
    ):
        monkeypatch.setattr(server_module, "ADMIN_PASSWORD", "")
        assert client.get("/v1/models").status_code == 200


class TestErrorShape:
    def test_openai_error_shape(self, client):
        resp = client.get("/v1/models")
        assert resp.status_code == 401
        payload = resp.get_json()
        assert payload["error"]["code"] == "invalid_api_key"
        assert isinstance(payload["error"]["message"], str)
        assert payload["error"]["message"]

    def test_anthropic_error_shape(self, client):
        resp = client.post("/v1/messages", json={"model": "claude-sonnet-4-5"})
        assert resp.status_code == 401
        payload = resp.get_json()
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "authentication_error"
        assert payload["error"]["message"]

    def test_anthropic_count_tokens_error_shape(self, client):
        resp = client.post("/v1/messages/count_tokens", json={"messages": []})
        assert resp.status_code == 401
        assert resp.get_json()["type"] == "error"

    def test_anthropic_disabled_key_uses_permission_error(self, client):
        entry = api_keys.create_key("测试")
        api_keys.update_key(entry["id"], enabled=False)
        resp = client.post(
            "/v1/messages",
            json={"model": "claude-sonnet-4-5"},
            headers={"x-api-key": entry["key"]},
        )
        assert resp.status_code == 403
        assert resp.get_json()["error"]["type"] == "permission_error"
