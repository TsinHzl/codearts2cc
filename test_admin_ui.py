# -*- coding: utf-8 -*-
"""admin_ui 单元测试：鉴权 / 模型分组 / 额度 / 配置写回。"""
import os

import pytest
from flask import Flask

import admin_ui
import usage_log


@pytest.fixture(autouse=True)
def _reset_global_state():
    """重置 admin_ui 的进程内退避与配置全局，避免测试间状态泄漏。"""
    admin_ui._fail_count = 0
    admin_ui._fail_until = 0.0
    admin_ui._lockout_rounds = 0
    admin_ui.admin_password = ""
    yield
    admin_ui._fail_count = 0
    admin_ui._fail_until = 0.0
    admin_ui._lockout_rounds = 0
    admin_ui.admin_password = ""


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
        env_file="/nonexistent/.env",
        upstream="https://example.com/api",
        host="127.0.0.1",
        port=8787,
        cache_path="/nonexistent/models-cache.json",
    )
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test-secret"
    app.register_blueprint(admin_ui.blueprint)
    app.testing = True
    return app


@pytest.fixture
def app_with_pw(tmp_path, monkeypatch):
    """已设置 ADMIN_PASSWORD + 临时 .env。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CODEARTS_AK=AK1234567890\n"
        "CODEARTS_SK=SKsecret999999\n"
        "CODEARTS_ANTHROPIC_MODEL_MAP=claude-sonnet-4-5:openpangu-2.0-pro\n"
        "CODEARTS_CACHE_SIM=1\n",
        encoding="utf-8",
    )
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
        env_file=str(env_file),
        upstream="https://example.com/api",
        host="0.0.0.0",
        port=8787,
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
        """已启用 + 未登录：GET /admin 壳页面放行（登录表单可加载），数据接口仍 401。"""
        client = app_with_pw.test_client()
        resp = client.get("/admin")
        assert resp.status_code == 200
        assert b"loginBtn" in resp.data
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

    def test_edit_map_writes_env(self, app_with_pw, monkeypatch):
        monkeypatch.delenv("CODEARTS_ANTHROPIC_MODEL_MAP", raising=False)  # teardown 还原进程环境
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_ANTHROPIC_MODEL_MAP": "new-alias:GLM-5.2"}
        })
        assert resp.status_code == 200
        # 文件行更新 AK/SK 行字节不变
        content = open(admin_ui.env_path, encoding="utf-8").read()
        assert "CODEARTS_AK=AK1234567890" in content
        assert "CODEARTS_SK=SKsecret999999" in content
        assert "CODEARTS_ANTHROPIC_MODEL_MAP=new-alias:GLM-5.2" in content
        assert os.environ["CODEARTS_ANTHROPIC_MODEL_MAP"] == "new-alias:GLM-5.2"

    def test_edit_unknown_key_rejected(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={"editable": {"CODEARTS_AK": "x"}})
        assert resp.status_code == 400

    def test_edit_nonexistent_key_rejected(self, app_with_pw):
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_ANTHROPIC_MODEL_MAP": "a:b",
                         "CODEARTS_CACHE_SIM_NOEXIST": "0"}
        })
        assert resp.status_code == 400

    def test_edit_multiline_value_rejected(self, app_with_pw):
        """CR 修复回归：值含换行（可伪造 .env 键行）必须被拒。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_CACHE_SIM": "0\nADMIN_PASSWORD=attacker"}
        })
        assert resp.status_code == 400
        # 文件未被改动
        content = open(admin_ui.env_path, encoding="utf-8").read()
        assert "ADMIN_PASSWORD=attacker" not in content

    def test_edit_non_string_value_rejected(self, app_with_pw):
        """CR 修复回归：非字符串值（bool/dict repr 污染 .env）必须被拒。"""
        client = app_with_pw.test_client()
        client.post("/api/admin/login", json={"password": "sekret"})
        resp = client.put("/api/admin/config", json={
            "editable": {"CODEARTS_CACHE_SIM": True}
        })
        assert resp.status_code == 400


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
# 使用日志 tab（HTML 壳）
# ---------------------------------------------------------------------------

class TestUsageTab:
    def test_admin_shell_contains_usage_tab(self, app_with_pw):
        """HTML 壳含「使用日志」tab 按钮与明细表关键列。"""
        client = app_with_pw.test_client()
        resp = client.get("/admin")
        html = resp.data.decode("utf-8")
        assert 'data-tab="usage"' in html
        assert "使用日志" in html
        assert "输入总计" in html
        assert "首字" in html
        assert "按模型分组" in html
        assert "/api/admin/usage" in html


# ---------------------------------------------------------------------------
# 使用日志
# ---------------------------------------------------------------------------

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
            assert data["total"] == 1
            assert data["entries"][0]["endpoint"] == "anthropic"
            # 分页参数生效
            data2 = client.get("/api/admin/usage?page=1&limit=200").get_json()
            assert data2["limit"] == 200
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)
