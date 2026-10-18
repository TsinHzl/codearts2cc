# -*- coding: utf-8 -*-
"""server.py 的 /oauth/callback 双阶段回调测试（design D7）。

覆盖：两阶段 happy path、307 目标校验、回环双重校验、候选逐个尝试、
无候选提示、响应头与 HTML 转义。沿用 test_api_key_gate.py 的做法：
import server 前注入假配置，避免模块级 AK/SK 校验与真实文件读写。
"""
import json
import os
import stat
import urllib.parse
from unittest import mock

import pytest

import api_keys
import config_store
import oauth_login


@pytest.fixture(scope="module")
def server_module(tmp_path_factory):
    """安全地 import server，并把 app 标记为 testing。

    server 模块级 oauth_login.init(OAUTH_CRED_PATH) 指向仓库路径，import 后
    立即重定向到临时目录，避免用例读写真实 oauth-auth.json。
    """
    cred_dir = tmp_path_factory.mktemp("oauth-callback")
    mp = pytest.MonkeyPatch()
    mp.setattr(config_store, "init", lambda path: None)
    mp.setattr(config_store, "get", lambda key, default="": {
        "CODEARTS_AK": "test-ak",
        "CODEARTS_SK": "test-sk",
        "CODEARTS_PROXY_PORT": "51818",
        "ADMIN_SECRET_KEY": "test-secret",
    }.get(key, default))
    mp.setattr(api_keys, "init", lambda path: None)
    # import 前一并屏蔽 oauth 模块级副作用，避免 import server 瞬间读取/写回
    # 本机真实 oauth-auth.json（server 模块级会调用 oauth_login.init(仓库路径)）
    mp.setattr(oauth_login, "init", lambda path: None)
    try:
        import server as mod
    finally:
        mp.undo()
    mod.oauth_login.init(str(cred_dir / "oauth-auth.json"))
    mod.app.testing = True
    yield mod
    mod.oauth_login.init(str(cred_dir / "unused.json"))


def _token_payload() -> dict:
    return {
        "user_id": "u1", "user_name": "tester", "domain_id": "d1",
        "refresh_token": "rt-1",
        "credentials": {
            "access_key_id": "ak-1", "secret_access_key": "sk-1",
            "security_token": "st-1", "expiration": "2036-10-18T12:00:00Z",
        },
    }


def _redirect_url(session) -> str:
    """构造合法的 portal 307 目标（redirect 参数）。"""
    query = urllib.parse.urlencode({"ticket_id": session.ticket_id})
    return f"{oauth_login.PORTAL_HOST}/oauth/callback?{query}"


def _create_session(server, admin="admin-A"):
    return server.oauth_sessions.create(port=51818, admin_session=admin)


@pytest.fixture(autouse=True)
def _fresh_store(server_module):
    """每个用例独立的会话仓库，避免跨用例状态泄漏。"""
    server_module.oauth_sessions = oauth_login.OAuthSessionStore()
    yield


class TestLoopbackGuard:
    def test_non_loopback_host_403(self, server_module):
        client = server_module.app.test_client()
        resp = client.get(
            "/oauth/callback?code=C",
            headers={"Host": "evil.example.com:51818"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 403
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["Referrer-Policy"] == "no-referrer"

    def test_non_loopback_remote_addr_403(self, server_module):
        client = server_module.app.test_client()
        resp = client.get(
            "/oauth/callback?code=C",
            environ_overrides={"REMOTE_ADDR": "203.0.113.9"},
        )
        assert resp.status_code == 403

    def test_ipv6_loopback_host_allowed(self, server_module):
        client = server_module.app.test_client()
        resp = client.get(
            "/oauth/callback",
            headers={"Host": "[::1]:51818"},
            environ_overrides={"REMOTE_ADDR": "::1"},
        )
        assert resp.status_code == 400  # 通过回环校验，因缺参数返回 400


class TestFirstStageSecret:
    def test_valid_secret_redirects_307(self, server_module):
        session = _create_session(server_module)
        client = server_module.app.test_client()
        redirect = _redirect_url(session)
        with mock.patch.object(oauth_login, "requests") as fake_requests:
            resp = client.get(
                f"/oauth/callback?secret=portal-S1&redirect={urllib.parse.quote(redirect, safe='')}",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert resp.status_code == 307
        # Location 由白名单参数重建（host + path + ticket_id），不回显原始 redirect
        parsed = urllib.parse.urlparse(resp.headers["Location"])
        assert parsed.scheme == "https"
        assert parsed.hostname == "codearts.huaweicloud.com"
        assert parsed.path == "/portal/oauth/callback"
        assert urllib.parse.parse_qs(parsed.query)["ticket_id"] == [session.ticket_id]
        fake_requests.assert_not_called()  # 首次回调为纯内存操作
        refreshed = server_module.oauth_sessions.get(session.id)
        assert refreshed.secret == "portal-S1" and refreshed.ticket_fallback is True

    @pytest.mark.parametrize("bad", [
        "http://codearts.huaweicloud.com/portal/oauth/callback?ticket_id=t",  # 非 https
        "https://evil.example.com/oauth/callback?ticket_id=t",               # 错 host
        "https://codearts.huaweicloud.com/portal/wrong/path?ticket_id=t",    # 错 path
        "https://user:pass@codearts.huaweicloud.com/portal/oauth/callback",  # 含 userinfo
    ])
    def test_invalid_redirect_target_400(self, server_module, bad):
        client = server_module.app.test_client()
        resp = client.get(
            f"/oauth/callback?secret=S&redirect={urllib.parse.quote(bad, safe='')}",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 400

    def test_malformed_redirect_url_400(self, server_module):
        """urlparse 抛 ValueError 的畸形输入（未闭合 IPv6 括号）不得 500。"""
        client = server_module.app.test_client()
        resp = client.get(
            "/oauth/callback?secret=S&redirect=https%3A%2F%2F%5B",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 400

    def test_crlf_in_redirect_query_400(self, server_module):
        """redirect 解码后含字面 CRLF（攻击者直接控制请求行）不得进入 Location 头。

        test_client 的 query_string 参数绕过外层二次编码，模拟攻击者
        原样发送含 %0D%0A 的请求行——request.args 解码后即字面 CRLF。
        """
        client = server_module.app.test_client()
        raw = ("secret=S&redirect=https://codearts.huaweicloud.com/portal"
               "/oauth/callback%3Fticket_id%3Dx%0D%0AX-Inject:%201")
        resp = client.get(
            f"/oauth/callback?{raw}",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 400
        assert "X-Inject" not in resp.headers.get("Location", "")

    def test_redirect_without_ticket_id_400(self, server_module):
        """缺 ticket_id 不得退化为覆写全部活跃会话 secret。"""
        session = _create_session(server_module)
        client = server_module.app.test_client()
        resp = client.get(
            "/oauth/callback?secret=evil&redirect="
            + urllib.parse.quote(
                f"{oauth_login.PORTAL_HOST}/oauth/callback", safe=""),
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 400
        refreshed = server_module.oauth_sessions.get(session.id)
        assert refreshed.secret != "evil" and refreshed.ticket_fallback is False


class TestSecondStageCode:
    def test_exchange_success_persists_and_completes(self, server_module):
        session = _create_session(server_module)
        client = server_module.app.test_client()
        cred_path = server_module.oauth_login._cred_path
        with mock.patch.object(
            oauth_login, "exchange_code", return_value=_token_payload(),
        ) as exchange:
            resp = client.get(
                f"/oauth/callback?code=C-1&secret={session.secret}",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert resp.status_code == 200
        assert "登录成功" in resp.get_data(as_text=True)
        exchange.assert_called_once()
        # 会话已结束、凭据已落盘（0600）
        assert server_module.oauth_sessions.get(session.id).done is True
        assert stat.S_IMODE(os.stat(cred_path).st_mode) == 0o600
        state = json.load(open(cred_path, "r", encoding="utf-8"))
        assert state["access_key_id"] == "ak-1"

    def test_candidates_tried_in_order_until_success(self, server_module):
        older = _create_session(server_module)
        newer = _create_session(server_module)
        client = server_module.app.test_client()
        # 倒序首个 newer 换码失败（PKCE 不匹配），第二个 older 成功
        with mock.patch.object(oauth_login, "exchange_code",
                               side_effect=[OSError("STS5.1805 mismatch"),
                                            _token_payload()]) as exchange:
            resp = client.get(
                "/oauth/callback?code=C-1",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert resp.status_code == 200
        assert exchange.call_count == 2  # PKCE 不匹配换下一个候选
        assert exchange.call_args_list[0].args[1] == newer.verifier
        assert exchange.call_args_list[1].args[1] == older.verifier
        assert server_module.oauth_sessions.get(older.id).done is True
        assert server_module.oauth_sessions.get(newer.id).done is False

    def test_all_candidates_fail_502_without_secrets(self, server_module):
        _create_session(server_module)
        client = server_module.app.test_client()
        with mock.patch.object(oauth_login, "exchange_code",
                               side_effect=OSError("STS5.1805: invalid_grant xyz")):
            resp = client.get(
                "/oauth/callback?code=C-1",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        body = resp.get_data(as_text=True)
        assert resp.status_code == 502
        assert "OSError" in body and "invalid_grant" not in body  # 只留类型名

    def test_no_candidates_returns_200_hint(self, server_module):
        client = server_module.app.test_client()
        with mock.patch.object(oauth_login, "exchange_code") as exchange:
            resp = client.get(
                "/oauth/callback?code=C-1",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert resp.status_code == 200
        assert "管理页" in resp.get_data(as_text=True)
        exchange.assert_not_called()

    def test_secret_miss_no_guess(self, server_module):
        """带 secret 但不命中（如会话已过期）：无候选，返回 200 提示。"""
        _create_session(server_module)
        client = server_module.app.test_client()
        with mock.patch.object(oauth_login, "exchange_code") as exchange:
            resp = client.get(
                "/oauth/callback?code=C-1&secret=stale-secret",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert resp.status_code == 200
        exchange.assert_not_called()


class TestContract:
    def test_missing_all_params_400(self, server_module):
        client = server_module.app.test_client()
        resp = client.get(
            "/oauth/callback", environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 400

    def test_html_escapes_params(self, server_module):
        """异常只保留类型名（不含消息），页面无任何未转义的参数文本。"""
        client = server_module.app.test_client()
        with mock.patch.object(
            oauth_login, "exchange_code",
            side_effect=ValueError("<script>alert(1)</script>"),
        ):
            _create_session(server_module)
            resp = client.get(
                "/oauth/callback?code=<img src=x onerror=alert(2)>",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        body = resp.get_data(as_text=True)
        assert resp.status_code == 502
        assert "ValueError" in body
        assert "<script>" not in body and "<img" not in body

    def test_save_failure_502(self, server_module):
        session = _create_session(server_module)
        client = server_module.app.test_client()
        with mock.patch.object(oauth_login, "exchange_code",
                               return_value=_token_payload()), \
             mock.patch.object(oauth_login, "save_credentials",
                               side_effect=OSError("disk full")):
            resp = client.get(
                f"/oauth/callback?code=C-1&secret={session.secret}",
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
        assert resp.status_code == 502
        assert "OSError" in resp.get_data(as_text=True)
