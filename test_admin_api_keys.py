# -*- coding: utf-8 -*-
"""API Key 管理端点单元测试（独立蓝图 admin_api_keys）。"""
import pytest

# 复用 admin_ui 测试的夹具：重置进程内全局、禁用管理面、已登录且带临时 config.json。
# pytest 会将被导入的 fixture 视为本模块定义的夹具。
from test_admin_ui import _reset_global_state, app_no_pw, app_with_pw  # noqa: F401

import api_keys
import config_store


@pytest.fixture
def admin_client(app_with_pw, tmp_path):
    """已登录的管理面客户端，API Key 存储指向用例独立文件。"""
    api_keys.init(str(tmp_path / "api-keys.json"))
    client = app_with_pw.test_client()
    client.post("/api/admin/login", json={"password": "sekret"})
    return client


class TestApiKeys:
    def test_requires_login(self, app_with_pw, tmp_path):
        api_keys.init(str(tmp_path / "api-keys.json"))
        resp = app_with_pw.test_client().get("/api/admin/api-keys")
        assert resp.status_code == 401

    def test_list_empty_reports_switch_on(self, admin_client):
        resp = admin_client.get("/api/admin/api-keys")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["keys"] == []
        assert payload["require_api_key"] == "1"

    def test_list_reports_switch_off(self, admin_client):
        config_store.set_many({"CODEARTS_REQUIRE_API_KEY": "0"})
        assert admin_client.get("/api/admin/api-keys").get_json()["require_api_key"] == "0"

    def test_create_returns_plaintext_and_persists(self, admin_client, tmp_path):
        resp = admin_client.post("/api/admin/api-keys", json={"name": "笔记本"})
        assert resp.status_code == 201
        entry = resp.get_json()["key"]
        assert entry["key"].startswith("sk-")
        assert entry["name"] == "笔记本"
        assert entry["enabled"] is True
        # 落盘后重新加载仍能读到同一明文（明文只在本地文件）
        api_keys.init(str(tmp_path / "api-keys.json"))
        assert [item["key"] for item in api_keys.list_keys()] == [entry["key"]]

    @pytest.mark.parametrize(
        "name",
        ["", "   ", "x" * (api_keys.NAME_MAX_LEN + 1), None, 123],
    )
    def test_create_rejects_invalid_name(self, admin_client, name):
        resp = admin_client.post("/api/admin/api-keys", json={"name": name})
        assert resp.status_code == 400
        assert api_keys.list_keys() == []

    def test_create_accepts_max_length_name(self, admin_client):
        resp = admin_client.post(
            "/api/admin/api-keys", json={"name": "x" * api_keys.NAME_MAX_LEN}
        )
        assert resp.status_code == 201

    def test_create_store_error_returns_500(self, admin_client, monkeypatch):
        """落盘失败转 500，且响应不得泄露底层异常内容。"""
        def boom(name):
            raise OSError("disk full at /secret/path")

        monkeypatch.setattr(api_keys, "create_key", boom)
        resp = admin_client.post("/api/admin/api-keys", json={"name": "测试"})
        assert resp.status_code == 500
        assert "disk full" not in resp.get_data(as_text=True)

    @pytest.mark.parametrize("body", [[1], "name", 123])
    def test_create_rejects_non_object_json(self, admin_client, body):
        """合法 JSON 但不是对象时必须 400，而不是因 .get() 抛异常变成 500。"""
        resp = admin_client.post("/api/admin/api-keys", json=body)
        assert resp.status_code == 400
        assert api_keys.list_keys() == []

    @pytest.mark.parametrize("body", [[1], "name", 123])
    def test_update_rejects_non_object_json(self, admin_client, body):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "原值"}
        ).get_json()["key"]
        resp = admin_client.patch(
            f"/api/admin/api-keys/{created['id']}", json=body
        )
        assert resp.status_code == 400
        assert api_keys.list_keys()[0]["name"] == "原值"

    @pytest.mark.parametrize("name", ["", "x" * (api_keys.NAME_MAX_LEN + 1), None])
    def test_patch_rejects_invalid_name(self, admin_client, name):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "原值"}
        ).get_json()["key"]
        resp = admin_client.patch(
            f"/api/admin/api-keys/{created['id']}", json={"name": name}
        )
        assert resp.status_code == 400
        assert api_keys.list_keys()[0]["name"] == "原值"

    def test_update_store_error_returns_500(self, admin_client, monkeypatch):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "测试"}
        ).get_json()["key"]

        def boom(key_id, name=None, enabled=None):
            raise OSError("disk full")

        monkeypatch.setattr(api_keys, "update_key", boom)
        resp = admin_client.patch(
            f"/api/admin/api-keys/{created['id']}", json={"name": "新名"}
        )
        assert resp.status_code == 500

    def test_delete_store_error_returns_500(self, admin_client, monkeypatch):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "测试"}
        ).get_json()["key"]

        def boom(key_id):
            raise OSError("disk full")

        monkeypatch.setattr(api_keys, "delete_key", boom)
        resp = admin_client.delete(f"/api/admin/api-keys/{created['id']}")
        assert resp.status_code == 500

    def test_rename(self, admin_client):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "旧名"}
        ).get_json()["key"]
        resp = admin_client.patch(
            f"/api/admin/api-keys/{created['id']}", json={"name": "新名"}
        )
        assert resp.status_code == 200
        assert resp.get_json()["key"]["name"] == "新名"

    def test_toggle_enabled(self, admin_client):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "测试"}
        ).get_json()["key"]
        resp = admin_client.patch(
            f"/api/admin/api-keys/{created['id']}", json={"enabled": False}
        )
        assert resp.status_code == 200
        assert resp.get_json()["key"]["enabled"] is False
        assert api_keys.list_keys()[0]["enabled"] is False

    @pytest.mark.parametrize("enabled", ["false", 0, 1, None])
    def test_toggle_rejects_non_boolean(self, admin_client, enabled):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "测试"}
        ).get_json()["key"]
        resp = admin_client.patch(
            f"/api/admin/api-keys/{created['id']}", json={"enabled": enabled}
        )
        assert resp.status_code == 400

    def test_patch_rejects_empty_body(self, admin_client):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "测试"}
        ).get_json()["key"]
        resp = admin_client.patch(f"/api/admin/api-keys/{created['id']}", json={})
        assert resp.status_code == 400

    def test_patch_unknown_id_404(self, admin_client):
        resp = admin_client.patch("/api/admin/api-keys/999", json={"name": "x"})
        assert resp.status_code == 404

    def test_delete(self, admin_client):
        created = admin_client.post(
            "/api/admin/api-keys", json={"name": "测试"}
        ).get_json()["key"]
        resp = admin_client.delete(f"/api/admin/api-keys/{created['id']}")
        assert resp.status_code == 200
        assert api_keys.list_keys() == []

    def test_delete_unknown_id_404(self, admin_client):
        assert admin_client.delete("/api/admin/api-keys/999").status_code == 404

    def test_disabled_management_returns_403(self, app_no_pw):
        resp = app_no_pw.test_client().get("/api/admin/api-keys")
        assert resp.status_code == 403
