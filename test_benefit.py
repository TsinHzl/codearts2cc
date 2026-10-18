# -*- coding: utf-8 -*-
"""benefit.py 签名头与 CLI 未登录降级测试（任务 9）。"""
from types import SimpleNamespace

import pytest

import benefit
import oauth_login


@pytest.fixture(autouse=True)
def _restore_oauth_state():
    """保存并恢复 oauth_login 全局态，避免污染其他测试模块。"""
    original = (
        oauth_login._cred_path,
        oauth_login._cred_state,
        oauth_login._cred_needs_relogin,
        oauth_login._cred_initialized,
    )
    yield
    with oauth_login._cred_lock:
        (oauth_login._cred_path, oauth_login._cred_state,
         oauth_login._cred_needs_relogin, oauth_login._cred_initialized) = original


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    """注入假已登录凭据，并把 CLI 凭据路径指向同一临时文件。"""
    path = tmp_path / "oauth-auth.json"
    oauth_login.init(str(path))
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
    monkeypatch.setattr(benefit, "OAUTH_CRED_PATH", str(path))
    return path


def _signed_names(headers: dict) -> set:
    return set(
        headers["Authorization"].split("SignedHeaders=")[1]
        .split(", Signature=")[0]
        .split(";")
    )


def test_signed_request_always_sends_content_sha256(logged_in, monkeypatch):
    with monkeypatch.context() as patch:
        sent = {}
        patch.setattr(benefit.requests, "request",
                      lambda *args, **kwargs: sent.update(kwargs["headers"]))
        benefit.signed_request("GET", benefit.CLAIM_URL)
    assert sent["x-sdk-content-sha256"] == oauth_login.content_sha256(b"")
    assert "x-sdk-content-sha256" in _signed_names(sent)


def test_signed_request_adds_security_token_when_present(logged_in, monkeypatch):
    with monkeypatch.context() as patch:
        sent = {}
        patch.setattr(benefit.requests, "request",
                      lambda *args, **kwargs: sent.update(kwargs["headers"]))
        benefit.signed_request("POST", benefit.CLAIM_URL, b"{}")
    assert sent["x-security-token"] == "st"
    assert "x-security-token" in _signed_names(sent)


def test_signed_request_omits_security_token_when_empty(monkeypatch):
    snap = SimpleNamespace(access_key_id="ak", secret_access_key="sk",
                           security_token="")
    monkeypatch.setattr(oauth_login, "current_credentials", lambda: snap)
    sent = {}
    monkeypatch.setattr(benefit.requests, "request",
                        lambda *args, **kwargs: sent.update(kwargs["headers"]))
    benefit.signed_request("GET", benefit.CLAIM_URL)
    assert "x-security-token" not in sent
    assert "x-security-token" not in _signed_names(sent)


def test_main_without_credentials_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(benefit, "OAUTH_CRED_PATH", str(tmp_path / "missing.json"))
    assert benefit.main(["benefit.py", "status"]) == 1
    assert "尚未登录" in capsys.readouterr().out
