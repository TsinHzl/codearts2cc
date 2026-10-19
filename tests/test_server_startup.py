# -*- coding: utf-8 -*-
"""server.py 启动路径与续期线程测试（tasks.md 任务 8 / design D9）。

覆盖：导入不起线程、启动刷新 10 秒超时与失败降级、reloader 进程判定、
续期线程唯一性与日志、未设 ADMIN_PASSWORD 的可操作警告、启动顺序。
沿用 test_oauth_callback.py 的 server_module fixture 模式。
"""
import threading

import pytest

from src.auth import api_keys
from src.config import config_store
from src.oauth import oauth_login


@pytest.fixture(scope="module")
def server_module(tmp_path_factory):
    """安全地 import server（屏蔽模块级文件读写），凭据指向临时目录。"""
    cred_dir = tmp_path_factory.mktemp("server-startup")
    mp = pytest.MonkeyPatch()
    mp.setattr(config_store, "init", lambda path: None)
    mp.setattr(config_store, "get", lambda key, default="": {
        "CODEARTS_PROXY_PORT": "51818",
        "ADMIN_SECRET_KEY": "test-secret",
    }.get(key, default))
    mp.setattr(api_keys, "init", lambda path: None)
    mp.setattr(oauth_login, "init", lambda path: None)
    try:
        from src import server as mod
    finally:
        mp.undo()
    mod.oauth_login.init(str(cred_dir / "oauth-auth.json"))
    mod.app.testing = True
    yield mod
    mod.oauth_login.init(str(cred_dir / "unused.json"))


def _oauth_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("oauth-")]


class TestImportAndGuard:
    def test_import_does_not_start_background_threads(self, server_module):
        assert _oauth_threads() == []

    def test_is_serving_process(self, server_module, monkeypatch):
        monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
        monkeypatch.setattr(server_module.app, "debug", False)
        assert server_module._is_serving_process() is True

        monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
        assert server_module._is_serving_process() is True

        monkeypatch.setenv("WERKZEUG_RUN_MAIN", "false")
        monkeypatch.setattr(server_module.app, "debug", True)
        assert server_module._is_serving_process() is False

        monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
        assert server_module._is_serving_process() is False


class TestStartupRefresh:
    def test_success_no_timeout_message(self, server_module, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(server_module.oauth_login, "refresh_if_needed",
                            lambda: calls.append(1) or "fresh")
        server_module._startup_refresh(timeout=2)
        assert calls == [1]
        assert "超过" not in capsys.readouterr().out

    def test_unavailable_prints_login_hint(self, server_module, monkeypatch, capsys):
        monkeypatch.setattr(server_module.oauth_login, "refresh_if_needed",
                            lambda: "unavailable")
        server_module._startup_refresh(timeout=2)
        assert "/admin" in capsys.readouterr().out

    def test_timeout_does_not_block_startup(self, server_module, monkeypatch, capsys):
        release = threading.Event()

        def slow():
            release.wait(5)
            return "fresh"

        monkeypatch.setattr(server_module.oauth_login, "refresh_if_needed", slow)
        server_module._startup_refresh(timeout=0.05)
        assert "已继续启动" in capsys.readouterr().out
        release.set()
        for worker in _oauth_threads():
            worker.join(2)

    def test_failure_prints_type_name_only(self, server_module, monkeypatch, capsys):
        def boom():
            raise OSError("rt-secret-value")

        monkeypatch.setattr(server_module.oauth_login, "refresh_if_needed", boom)
        server_module._startup_refresh(timeout=2)
        out = capsys.readouterr().out
        assert "OSError" in out
        assert "rt-secret-value" not in out


class TestRenewalThread:
    def test_serving_process_starts_single_daemon_thread(self, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "_is_serving_process", lambda: True)
        monkeypatch.setattr(server_module, "_renewal_loop", lambda interval: None)
        worker = server_module._start_renewal_thread()
        assert worker is not None and worker.daemon is True
        assert worker.name == "oauth-renewal"
        worker.join(2)
        assert not worker.is_alive()

    def test_reloader_parent_skips_thread(self, server_module, monkeypatch):
        monkeypatch.setattr(server_module, "_is_serving_process", lambda: False)
        assert server_module._start_renewal_thread() is None

    def test_reloader_scenario_only_child_starts(self, server_module, monkeypatch):
        """父进程（debug=True、无 WERKZEUG_RUN_MAIN）跳过，子进程启动。"""
        monkeypatch.setattr(server_module, "_renewal_loop", lambda interval: None)
        monkeypatch.setattr(server_module.app, "debug", True)
        monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
        assert server_module._start_renewal_thread() is None
        monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
        worker = server_module._start_renewal_thread()
        assert worker is not None
        worker.join(2)

    def test_loop_logs_relogin_and_continues(self, server_module, monkeypatch, capsys):
        stop = threading.Event()
        calls = []

        def fake_refresh():
            calls.append(1)
            if len(calls) == 1:
                return "relogin"
            stop.set()
            return "fresh"

        monkeypatch.setattr(server_module.oauth_login, "refresh_if_needed", fake_refresh)
        server_module._renewal_loop(interval=0.01, stop_event=stop)
        assert len(calls) == 2
        assert "重新登录" in capsys.readouterr().out

    def test_loop_survives_exception(self, server_module, monkeypatch, capsys):
        stop = threading.Event()
        calls = []

        def fake_refresh():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("upstream-secret")
            stop.set()
            return "fresh"

        monkeypatch.setattr(server_module.oauth_login, "refresh_if_needed", fake_refresh)
        server_module._renewal_loop(interval=0.01, stop_event=stop)
        out = capsys.readouterr().out
        assert len(calls) == 2
        assert "OSError" in out and "upstream-secret" not in out


class TestMain:
    def test_startup_sequence_starts_thread(self, server_module, monkeypatch):
        calls = []
        monkeypatch.setattr(server_module, "_is_serving_process", lambda: True)
        monkeypatch.setattr(server_module, "_startup_refresh",
                            lambda: calls.append("refresh"))
        monkeypatch.setattr(server_module, "_refresh_models", lambda: True)
        monkeypatch.setattr(server_module.usage_log, "init",
                            lambda: calls.append("usage"))
        monkeypatch.setattr(server_module, "_start_renewal_thread",
                            lambda: calls.append("thread"))
        monkeypatch.setattr(server_module.app, "run",
                            lambda **kwargs: calls.append("run"))
        monkeypatch.setattr(server_module, "ADMIN_PASSWORD", "")
        server_module._main()
        assert calls == ["refresh", "usage", "thread", "run"]

    def test_reloader_parent_skips_refresh_and_thread(self, server_module, monkeypatch):
        calls = []
        monkeypatch.setattr(server_module, "_is_serving_process", lambda: False)
        monkeypatch.setattr(server_module, "_startup_refresh",
                            lambda: calls.append("refresh"))
        monkeypatch.setattr(server_module, "_refresh_models", lambda: True)
        monkeypatch.setattr(server_module.usage_log, "init",
                            lambda: calls.append("usage"))
        monkeypatch.setattr(server_module, "_start_renewal_thread",
                            lambda: calls.append("thread"))
        monkeypatch.setattr(server_module.app, "run",
                            lambda **kwargs: calls.append("run"))
        monkeypatch.setattr(server_module, "ADMIN_PASSWORD", "")
        server_module._main()
        assert calls == ["usage", "run"]

    def test_missing_password_warning_is_actionable(self, server_module, capsys):
        server_module._warn_missing_admin_password()
        out = capsys.readouterr().out
        assert "ADMIN_PASSWORD" in out
        assert "config.json" in out and "/admin" in out
