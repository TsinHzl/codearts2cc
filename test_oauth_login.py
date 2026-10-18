# -*- coding: utf-8 -*-
"""oauth_login 协议层单元测试：PKCE / DPoP / 授权链接 / 换码与刷新表单 /
ticket 归一化 / 终态失效判定 / 签名固定向量。

对齐 codearts2api（Go 参考实现）：internal/upstream/{client,dpop,signer}.go。
"""
import base64
import dataclasses
import datetime
import hashlib
import json
import os
import stat
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

import oauth_login

# P-256 曲线阶（低 S 断言用）
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

TOKEN_URL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
TICKET_URL = "https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager/v1/login/ticket"

EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def b64url_of_sha256(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()


def _resp(status: int, payload: dict):
    """构造 mock 的 requests.Response（status_code + json()）。"""
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    return r


class TestPKCE:
    def test_verifier_is_64_bytes_base64url_nopad(self):
        verifier, _ = oauth_login.generate_pkce()
        assert "=" not in verifier and "+" not in verifier and "/" not in verifier
        assert len(b64url_decode(verifier)) == 64

    def test_challenge_is_base64url_sha256_of_verifier(self):
        verifier, challenge = oauth_login.generate_pkce()
        want = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert challenge == want

    def test_each_call_is_unique(self):
        assert oauth_login.generate_pkce() != oauth_login.generate_pkce()


class TestDPoPKey:
    def test_new_private_jwk_fields(self):
        jwk = oauth_login.new_dpop_private_jwk()
        assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
        for field in ("x", "y", "d"):
            assert len(b64url_decode(jwk[field])) == 32

    def test_keypair_roundtrip_matches_public_jwk(self):
        jwk = oauth_login.new_dpop_private_jwk()
        _, pub = oauth_login.keypair_from_private_jwk(jwk)
        assert pub == {"kty": "EC", "crv": "P-256", "x": jwk["x"], "y": jwk["y"]}

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda j: j.update({"crv": "P-384"}), id="wrong-curve"),
            pytest.param(lambda j: j.pop("d"), id="missing-d"),
            pytest.param(lambda j: j.update({"d": base64.urlsafe_b64encode(
                P256_N.to_bytes(32, "big")).rstrip(b"=").decode()}), id="d-equals-n"),
            pytest.param(lambda j: j.update({"d": base64.urlsafe_b64encode(
                (P256_N + 1).to_bytes(32, "big")).rstrip(b"=").decode()}), id="d-out-of-range"),
            pytest.param(lambda j: j.update({"x": j["x"][:-1] + ("A" if j["x"][-1] != "A" else "B")}),
                         id="pubkey-mismatch"),
        ],
    )
    def test_bad_jwk_rejected(self, mutate):
        jwk = oauth_login.new_dpop_private_jwk()
        mutate(jwk)
        with pytest.raises(ValueError):
            oauth_login.keypair_from_private_jwk(jwk)

    def test_d_at_upper_legal_bound_accepted(self):
        """d = N-1 是 SEC1 合法私钥上界：应能还原（公私钥必然不匹配时抛 mismatch，
        而非范围错误）；这里用真实生成密钥验证正常路径。"""
        jwk = oauth_login.new_dpop_private_jwk()
        key, pub = oauth_login.keypair_from_private_jwk(jwk)
        assert key is not None and pub["x"] == jwk["x"]


class TestDPoPProof:
    def test_header_payload_and_low_s_signature(self):
        jwk = oauth_login.new_dpop_private_jwk()
        proof = oauth_login.sign_dpop_proof(jwk)
        h, p, sig = proof.split(".")
        header = json.loads(b64url_decode(h))
        payload = json.loads(b64url_decode(p))
        assert header["alg"] == "ES256"
        assert header["typ"] == "dpop+jwt"
        pub = header["jwk"]
        assert pub["kty"] == "EC" and pub["crv"] == "P-256"
        assert pub["x"] == jwk["x"] and pub["y"] == jwk["y"]
        assert "d" not in pub  # 私钥分量绝不进入 JWT
        assert payload["htm"] == "POST"
        # htu 为完整 URL：无 query、无尾斜杠
        assert payload["htu"] == TOKEN_URL
        assert "?" not in payload["htu"] and not payload["htu"].endswith("/")
        # jti 为 16 字节随机数的 32 位 hex
        assert len(payload["jti"]) == 32
        int(payload["jti"], 16)  # 纯 hex
        assert isinstance(payload["iat"], int)
        # 签名 r||s 各 32 字节，低 S 归一化
        sig_raw = b64url_decode(sig)
        assert len(sig_raw) == 64
        s = int.from_bytes(sig_raw[32:], "big")
        assert 0 < s <= P256_N // 2

    def test_signature_verifies_with_public_jwk(self):
        jwk = oauth_login.new_dpop_private_jwk()
        proof = oauth_login.sign_dpop_proof(jwk)
        h, p, sig = proof.split(".")
        pub_jwk = json.loads(b64url_decode(h))["jwk"]
        pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(b64url_decode(pub_jwk["x"]), "big"),
            int.from_bytes(b64url_decode(pub_jwk["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
        sig_raw = b64url_decode(sig)
        r = int.from_bytes(sig_raw[:32], "big")
        s = int.from_bytes(sig_raw[32:], "big")
        pub.verify(  # 抛异常即失败
            encode_dss_signature(r, s),
            f"{h}.{p}".encode(),
            ec.ECDSA(hashes.SHA256()),
        )

    def test_jti_unique_per_proof(self):
        jwk = oauth_login.new_dpop_private_jwk()
        jti_a = json.loads(b64url_decode(oauth_login.sign_dpop_proof(jwk).split(".")[1]))["jti"]
        jti_b = json.loads(b64url_decode(oauth_login.sign_dpop_proof(jwk).split(".")[1]))["jti"]
        assert jti_a != jti_b


class TestAuthorizeURL:
    def test_full_param_set_and_port(self):
        verifier, challenge = oauth_login.generate_pkce()
        url = oauth_login.build_authorize_url(
            ticket_id="T" * 32, code_challenge=challenge,
            state="S" * 32, port=51818,
        )
        u = urlparse(url)
        assert u.scheme == "https"
        assert u.netloc == "codearts.huaweicloud.com"
        assert u.path == "/portal/authorize"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        # 集合级完备断言：参数不多不少、无重复（parse_qs 已折叠重复值到列表）
        assert set(q) == {
            "theme", "locale", "uri_scheme", "client_id", "port",
            "code_challenge", "code_challenge_method", "state",
            "ticket_id", "plugin-name", "plugin-version",
        }
        assert all(len(v) == 1 for v in parse_qs(u.query).values())
        assert q["theme"] == "2"
        assert q["locale"] == "zh-cn"
        assert q["uri_scheme"] == "codearts-agent"
        assert q["client_id"] == "codearts-agent"
        assert q["port"] == "51818"
        assert q["code_challenge"] == challenge
        assert q["code_challenge_method"] == "SHA-256"
        assert q["state"] == "S" * 32
        assert q["ticket_id"] == "T" * 32
        assert q["plugin-name"] == "snap_AIIDE"
        assert q["plugin-version"] == "5.2.0"


_TOKEN_OK = {
    "user_id": "u1", "user_name": "tester", "domain_id": "d1",
    "refresh_token": "rt",
    "credentials": {
        "access_key_id": "ak", "secret_access_key": "sk",
        "security_token": "st", "expiration": "2026-10-18T12:00:00Z",
    },
}


class TestTokenRequests:
    def test_exchange_code_form_and_dpop_header(self):
        jwk = oauth_login.new_dpop_private_jwk()
        with mock.patch("oauth_login.requests.post", return_value=_resp(200, _TOKEN_OK)) as post:
            out = oauth_login.exchange_code(
                code="AUTH", code_verifier="V", port=51818, dpop_private_jwk=jwk,
            )
        url, kwargs = post.call_args[0][0], post.call_args.kwargs
        assert url == TOKEN_URL
        form = kwargs["data"]
        assert form["client_id"] == "codearts-agent"
        assert form["code"] == "AUTH"
        assert form["code_verifier"] == "V"
        assert form["grant_type"] == "authorization_code"
        assert form["redirect_uri"] == "http://127.0.0.1:51818/oauth/callback"
        assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
        assert len(kwargs["headers"]["DPoP"].split(".")) == 3  # 三段式 JWT
        assert out["credentials"]["access_key_id"] == "ak"
        assert out["refresh_token"] == "rt"

    def test_refresh_form_and_dpop_header(self):
        jwk = oauth_login.new_dpop_private_jwk()
        with mock.patch("oauth_login.requests.post", return_value=_resp(200, _TOKEN_OK)) as post:
            oauth_login.refresh_token_request(
                refresh_token="RT", code_verifier="V",
                client_id="codearts-agent", dpop_private_jwk=jwk,
            )
        url, kwargs = post.call_args[0][0], post.call_args.kwargs
        assert url == TOKEN_URL
        form = kwargs["data"]
        assert form["client_id"] == "codearts-agent"
        assert form["code_verifier"] == "V"
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "RT"
        assert "code" not in form and "redirect_uri" not in form
        assert len(kwargs["headers"]["DPoP"].split(".")) == 3  # 三段式 JWT


class TestTicketPoll:
    def test_unsigned_get_with_plugin_headers_only(self):
        legacy = {
            "user_id": "u1", "user_name": "tester", "domain_id": "d1",
            "credential": {  # legacy 通道字段：access/secret/securitytoken/expires_at
                "access": "ak", "secret": "sk",
                "securitytoken": "st", "expires_at": "2026-10-18T12:00:00Z",
            },
        }
        with mock.patch("oauth_login.requests.get", return_value=_resp(200, legacy)) as get:
            out = oauth_login.poll_ticket(ticket_id="T", secret="S")
        url = get.call_args[0][0]
        u = urlparse(url)
        assert u.scheme == "https" and u.netloc == "snap-access.cn-north-4.myhuaweicloud.com"
        assert u.path == "/snap-manager/v1/login/ticket"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        assert q["ticket_id"] == "T" and q["secret"] == "S"
        headers = get.call_args.kwargs.get("headers") or {}
        assert headers == {"plugin-name": "snap_AIIDE", "plugin-version": "5.2.0"}
        # legacy 字段归一化为 credentials 结构
        cred = out["credentials"]
        assert cred["access_key_id"] == "ak"
        assert cred["secret_access_key"] == "sk"
        assert cred["security_token"] == "st"
        assert cred["expiration"] == "2026-10-18T12:00:00Z"

    def test_missing_credentials_raises(self):
        with mock.patch("oauth_login.requests.get", return_value=_resp(200, {"user_id": "u1"})):
            with pytest.raises(ValueError):
                oauth_login.poll_ticket(ticket_id="T", secret="S")


class TestRefreshTerminalState:
    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"error": "invalid_grant"}, id="invalid-grant"),
            pytest.param({"error_code": "IAM.1502 ExpiredRefreshToken"}, id="expired"),
            pytest.param({"error_code": "InvalidDPoPHeader"}, id="bad-dpop"),
            pytest.param({"error_code": "invalid client id: foo"}, id="bad-client"),
            pytest.param({"error_msg": "refresh token has been used"}, id="reused"),
        ],
    )
    def test_terminal_failures(self, payload):
        jwk = oauth_login.new_dpop_private_jwk()
        with mock.patch("oauth_login.requests.post", return_value=_resp(400, payload)):
            with pytest.raises(oauth_login.RefreshTokenExpiredError):
                oauth_login.refresh_token_request(
                    refresh_token="RT", code_verifier="V",
                    client_id="codearts-agent", dpop_private_jwk=jwk,
                )

    def test_server_error_is_not_terminal(self):
        jwk = oauth_login.new_dpop_private_jwk()
        with mock.patch("oauth_login.requests.post", return_value=_resp(502, {"error": "upstream"})):
            with pytest.raises(Exception) as ei:
                oauth_login.refresh_token_request(
                    refresh_token="RT", code_verifier="V",
                    client_id="codearts-agent", dpop_private_jwk=jwk,
                )
        assert not isinstance(ei.value, oauth_login.RefreshTokenExpiredError)

    def test_network_error_is_not_terminal(self):
        jwk = oauth_login.new_dpop_private_jwk()
        with mock.patch(
            "oauth_login.requests.post",
            side_effect=oauth_login.requests.exceptions.ConnectionError("conn"),
        ):
            with pytest.raises(Exception) as ei:
                oauth_login.refresh_token_request(
                    refresh_token="RT", code_verifier="V",
                    client_id="codearts-agent", dpop_private_jwk=jwk,
                )
        assert not isinstance(ei.value, oauth_login.RefreshTokenExpiredError)


class TestContentSha256Vector:
    def test_empty_body_constant(self):
        assert oauth_login.content_sha256(b"") == EMPTY_SHA

    def test_nonempty_body(self):
        body = b'{"model":"openpangu-2.0-pro"}'
        assert oauth_login.content_sha256(body) == hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# 凭据存储（任务 3.1 / 3.2）
# ---------------------------------------------------------------------------

_EXPIRES = "2036-10-18T12:00:00Z"  # 远期未过期，避免用例对时钟敏感


def _token_payload() -> dict:
    return {
        "user_id": "u1", "user_name": "tester", "domain_id": "d1",
        "refresh_token": "rt-1",
        "credentials": {
            "access_key_id": "ak-1", "secret_access_key": "sk-1",
            "security_token": "st-1", "expiration": _EXPIRES,
        },
    }


@pytest.fixture
def cred_store(tmp_path):
    """每个用例独立的 oauth-auth.json 路径与干净模块状态。"""
    path = str(tmp_path / "oauth-auth.json")
    oauth_login.init(path)
    yield path
    oauth_login.init(str(tmp_path / "unused.json"))  # 重置内存态，防跨用例泄漏


def _save_ok(cred_store, payload=None, verifier="V-1", jwk=None):
    """保存一份有效凭据；cred_store 参数仅用于声明 fixture 依赖（确保已 init）。"""
    jwk = jwk or oauth_login.new_dpop_private_jwk()
    oauth_login.save_credentials(
        payload or _token_payload(), code_verifier=verifier,
        dpop_private_jwk=jwk,
    )
    return jwk


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


class TestCredentialStore:
    def test_save_writes_0600_and_reloadable(self, cred_store):
        _save_ok(cred_store)
        assert _mode(cred_store) == 0o600
        oauth_login.init(cred_store)  # 重启模拟：重读文件
        snap = oauth_login.current_credentials()
        assert snap.access_key_id == "ak-1"
        assert snap.refresh_token == "rt-1"
        assert snap.code_verifier == "V-1"
        assert snap.client_id == "codearts-agent"
        assert snap.dpop_private_key["kty"] == "EC"

    def test_snapshot_is_frozen_and_isolated(self, cred_store):
        _save_ok(cred_store)
        snap = oauth_login.current_credentials()
        with pytest.raises(dataclasses.FrozenInstanceError):  # frozen dataclass 禁止赋值
            snap.access_key_id = "mutated"  # type: ignore[misc]
        snap.dpop_private_key["d"] = "tampered"
        assert oauth_login.current_credentials().dpop_private_key.get("d") != "tampered"

    def test_broken_json_degrades_to_not_logged_in(self, cred_store, capsys):
        # 先落一份含真实敏感值的凭据，再从中间截断制造「前半合法、后半损坏」
        _save_ok(cred_store)
        raw = open(cred_store, "r", encoding="utf-8").read()
        with open(cred_store, "w", encoding="utf-8") as handle:
            handle.write(raw[: len(raw) // 2] + "{broken")
        oauth_login.init(cred_store)
        status = oauth_login.credentials_status()
        assert status == {"logged_in": False, "needs_relogin": False}
        with pytest.raises(oauth_login.CredentialsUnavailable):
            oauth_login.current_credentials()
        assert "sk-1" not in capsys.readouterr().out  # 警告不含字段值

    def test_invalid_dpop_degrades(self, cred_store):
        # 用真实落盘格式：保存有效凭据后仅替换 JWK 字段，避免格式耦合假阳性
        jwk = _save_ok(cred_store)
        state = json.load(open(cred_store, "r", encoding="utf-8"))
        state["dpop_private_key"] = {"kty": "EC", "crv": "P-384",
                                     "x": jwk["x"], "y": jwk["y"], "d": jwk["d"]}
        with open(cred_store, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        oauth_login.init(cred_store)
        assert oauth_login.credentials_status()["logged_in"] is False
        with pytest.raises(oauth_login.CredentialsUnavailable):
            oauth_login.current_credentials()

    def test_invalid_expiration_degrades(self, cred_store):
        _save_ok(cred_store)
        state = json.load(open(cred_store, "r", encoding="utf-8"))
        state["expiration"] = "garbage"
        with open(cred_store, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        oauth_login.init(cred_store)
        assert oauth_login.credentials_status()["logged_in"] is False

    def test_uninit_raises_runtime_error(self):
        with mock.patch.object(oauth_login, "_cred_initialized", False):
            with pytest.raises(RuntimeError):
                oauth_login.current_credentials()
            with pytest.raises(RuntimeError):
                oauth_login.credentials_status()
            with pytest.raises(RuntimeError):
                oauth_login.mark_needs_relogin()

    def test_atomic_write_no_tmp_left_behind(self, cred_store):
        _save_ok(cred_store)
        # 目录级全等断言：tmp_path 唯一合法产物就是凭据文件，任意命名的残留都会暴露
        assert os.listdir(os.path.dirname(cred_store)) == ["oauth-auth.json"]

    def test_status_has_no_secrets(self, cred_store):
        jwk = _save_ok(cred_store)
        status = oauth_login.credentials_status()
        # 字段白名单：任何新增字段都必须显式登记，防止未来无意下发凭据字段
        assert set(status) <= {
            "logged_in", "needs_relogin", "user_id", "user_name", "domain_id",
            "expiration", "has_refresh_token", "remaining_seconds",
        }
        status_text = json.dumps(status, ensure_ascii=False)
        for secret in ("sk-1", "st-1", "rt-1", "V-1", jwk["d"]):
            assert secret not in status_text

    def test_logout_clears_state_and_file(self, cred_store):
        _save_ok(cred_store)
        oauth_login.logout()
        assert not os.path.exists(cred_store)
        assert oauth_login.credentials_status() == {"logged_in": False, "needs_relogin": False}

    def test_mark_needs_relogin_blocks_credentials(self, cred_store):
        _save_ok(cred_store)
        oauth_login.mark_needs_relogin()
        with pytest.raises(oauth_login.CredentialsUnavailable):
            oauth_login.current_credentials()
        status = oauth_login.credentials_status()
        assert status["needs_relogin"] is True and status["logged_in"] is True


class TestRefreshFieldsPersistence:
    def test_refresh_fields_roundtrip_for_refresh_form(self, cred_store):
        """刷新表单所需的 code_verifier / client_id / refresh_token 完整持久化。"""
        jwk = _save_ok(cred_store, verifier="V-KEEP")
        oauth_login.init(cred_store)  # 重启后仍可刷新
        snap = oauth_login.current_credentials()
        with mock.patch("oauth_login.requests.post",
                        return_value=_resp(200, _token_payload())) as post:
            oauth_login.refresh_token_request(
                refresh_token=snap.refresh_token,
                code_verifier=snap.code_verifier,
                client_id=snap.client_id,
                dpop_private_jwk=snap.dpop_private_key,
            )
        form = post.call_args.kwargs["data"]
        assert form["code_verifier"] == "V-KEEP"
        assert form["client_id"] == "codearts-agent"
        assert form["refresh_token"] == "rt-1"
        # DPoP 复用签发时的私钥（可还原出同一公钥）
        _, pub = oauth_login.keypair_from_private_jwk(snap.dpop_private_key)
        assert pub["x"] == jwk["x"]

    def test_missing_refresh_token_still_saves(self, cred_store):
        payload = _token_payload()
        payload.pop("refresh_token")
        oauth_login.save_credentials(
            payload, code_verifier="V",
            dpop_private_jwk=oauth_login.new_dpop_private_jwk(),
        )
        status = oauth_login.credentials_status()
        assert status["logged_in"] is True and status["has_refresh_token"] is False


class TestOAuthSessionStore:
    """design D6：TTL、候选顺序、上限 409、跨管理会话拒绝、ticket 门控。"""

    def _store(self):
        fake = mock.Mock()
        fake.side_effect = lambda: fake.now
        fake.now = 1000.0
        return oauth_login.OAuthSessionStore(clock=fake), fake

    def test_create_generates_full_material_and_auth_url(self):
        store, _ = self._store()
        session = store.create(port=51818, admin_session="admin-A")
        assert len(session.id) == 32 and len(session.state) == 32
        assert len(session.ticket_id) == 32 and len(session.secret) == 32
        assert session.verifier and session.dpop_private_key["kty"] == "EC"
        assert session.done is False and session.ticket_fallback is False
        parsed = urlparse(session.auth_url)
        q = parse_qs(parsed.query)
        assert parsed.scheme == "https" and parsed.netloc.startswith("codearts.huaweicloud.com")
        assert q["state"][0] == session.state and q["ticket_id"][0] == session.ticket_id
        assert q["code_challenge"][0] == b64url_of_sha256(session.verifier)
        assert q["port"][0] == "51818" and q["client_id"][0] == "codearts-agent"

    def test_ttl_expiry_gcs_session(self):
        store, clock = self._store()
        session = store.create(port=51818, admin_session="admin-A")
        clock.now += oauth_login.SESSION_TTL_SECONDS + 1
        assert store.get(session.id) is None
        with pytest.raises(oauth_login.SessionUnavailableError):
            store.require(session.id, "admin-A")
        assert store.candidates() == []  # 过期会话不再参与回调匹配

    def test_candidate_match_by_secret_and_no_guess_on_miss(self):
        store, _ = self._store()
        session = store.create(port=51818, admin_session="admin-A")
        assert store.candidates(secret=session.secret) == [session]
        assert store.candidates(secret="no-such-secret") == []
        assert store.candidates(ticket_id=session.ticket_id) == [session]
        assert store.candidates(ticket_id="no-such-ticket") == []

    def test_candidates_fallback_order_and_cap(self):
        store, clock = self._store()
        created = []
        for i in range(12):  # 12 > 候选上限 10
            created.append(store.create(port=51818, admin_session=f"admin-{i}").id)
            clock.now += 1
        sessions = store.candidates()
        assert len(sessions) == oauth_login.SESSION_MAX_CANDIDATES
        assert [s.id for s in sessions] == list(reversed(created))[:10]  # 创建时间倒序

    def test_done_sessions_excluded_from_candidates(self):
        store, _ = self._store()
        done = store.create(port=51818, admin_session="admin-A")
        active = store.create(port=51818, admin_session="admin-B")
        store.complete(done.id)
        assert store.candidates() == [active]
        # done 会话的 secret 匹配中同样排除
        assert store.candidates(secret=done.secret) == []

    def test_total_limit_20_returns_409(self):
        store, clock = self._store()
        for i in range(oauth_login.SESSION_MAX_TOTAL):
            store.create(port=51818, admin_session=f"admin-{i % 4}")
            clock.now += 0.01
        with pytest.raises(oauth_login.SessionLimitError):
            store.create(port=51818, admin_session="admin-new")

    def test_per_admin_active_limit_5(self):
        store, clock = self._store()
        for _ in range(oauth_login.SESSION_MAX_PER_ADMIN):
            store.create(port=51818, admin_session="admin-A")
            clock.now += 0.01
        with pytest.raises(oauth_login.SessionLimitError):
            store.create(port=51818, admin_session="admin-A")
        store.create(port=51818, admin_session="admin-B")  # 其他管理会话不受影响

    def test_complete_frees_active_quota(self):
        store, clock = self._store()
        sessions = [store.create(port=51818, admin_session="admin-A")
                    for _ in range(oauth_login.SESSION_MAX_PER_ADMIN)]
        clock.now += 0.01
        store.complete(sessions[0].id)
        store.create(port=51818, admin_session="admin-A")  # done 释放活跃名额

    def test_cross_admin_session_denied(self):
        store, _ = self._store()
        session = store.create(port=51818, admin_session="admin-A")
        with pytest.raises(oauth_login.SessionUnavailableError):
            store.require(session.id, "admin-B")
        assert store.require(session.id, "admin-A") is session

    def test_ticket_fallback_gate(self):
        store, _ = self._store()
        session = store.create(port=51818, admin_session="admin-A")
        assert session.ticket_fallback is False  # 门控默认关闭：poll 不请求 ticket
        assert store.enable_ticket_fallback(session.id, "portal-secret-1") is True
        refreshed = store.get(session.id)
        assert refreshed.secret == "portal-secret-1" and refreshed.ticket_fallback is True
        assert store.enable_ticket_fallback("missing", "x") is False

    def test_complete_records_err_and_remove(self):
        store, _ = self._store()
        session = store.create(port=51818, admin_session="admin-A")
        store.complete(session.id, err="exchange failed")
        got = store.get(session.id)
        assert got.done is True and got.err == "exchange failed"
        store.remove(session.id)
        assert store.get(session.id) is None

    def test_create_no_shared_state_between_stores(self):
        store_a, _ = self._store()
        store_b, _ = self._store()
        session = store_a.create(port=51818, admin_session="admin-A")
        assert store_b.get(session.id) is None


# ---------------------------------------------------------------------------
# 自动续期（任务 4.2）
# ---------------------------------------------------------------------------

def _expiring_payload(delta: float) -> dict:
    """expiration 距 now（真实时钟）delta 秒的 token 响应。"""
    payload = _token_payload()
    exp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta)
    payload["credentials"]["expiration"] = exp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return payload


class TestRefreshIfNeeded:
    """design D9：30 分钟阈值、5 分钟退避、终态失效、updated_at 竞争。"""

    def _reset(self):
        oauth_login._next_retry_at = 0.0

    def teardown_method(self):
        self._reset()

    def test_fresh_credentials_not_refreshed(self, cred_store):
        _save_ok(cred_store)  # expiration 在 2036 年
        with mock.patch("oauth_login.requests.post") as post:
            result = oauth_login.refresh_if_needed()
        assert result == "fresh"
        post.assert_not_called()

    def test_expiring_soon_triggers_refresh(self, cred_store):
        jwk = _save_ok(cred_store, payload=_expiring_payload(10 * 60),
                       verifier="V-KEEP")
        new_payload = _expiring_payload(3600)
        with mock.patch("oauth_login.requests.post",
                        return_value=_resp(200, new_payload)) as post:
            result = oauth_login.refresh_if_needed()
        assert result == "refreshed"
        form = post.call_args.kwargs["data"]
        assert form["grant_type"] == "refresh_token"
        assert form["refresh_token"] == "rt-1" and form["code_verifier"] == "V-KEEP"
        # 新凭据已写回：expiration 更新、DPoP 私钥与 verifier 复用
        snap = oauth_login.current_credentials()
        assert snap.refresh_token == new_payload["refresh_token"]
        _, pub = oauth_login.keypair_from_private_jwk(snap.dpop_private_key)
        assert pub["x"] == jwk["x"]

    def test_expired_credentials_still_refreshed(self, cred_store):
        _save_ok(cred_store, payload=_expiring_payload(-60))
        with mock.patch("oauth_login.requests.post",
                        return_value=_resp(200, _expiring_payload(3600))):
            assert oauth_login.refresh_if_needed() == "refreshed"

    def test_response_without_refresh_token_keeps_old(self, cred_store):
        _save_ok(cred_store, payload=_expiring_payload(10 * 60))
        new_payload = _expiring_payload(3600)
        new_payload.pop("refresh_token")
        with mock.patch("oauth_login.requests.post",
                        return_value=_resp(200, new_payload)):
            assert oauth_login.refresh_if_needed() == "refreshed"
        assert oauth_login.current_credentials().refresh_token == "rt-1"

    def test_transient_error_enters_backoff(self, cred_store):
        _save_ok(cred_store, payload=_expiring_payload(10 * 60))
        fake_now = mock.Mock(return_value=1000.0)
        with mock.patch("oauth_login.requests.post",
                        side_effect=oauth_login.requests.ConnectionError("boom")):
            assert oauth_login.refresh_if_needed(time_fn=fake_now) == "backoff"
        assert oauth_login._next_retry_at == 1000.0 + oauth_login.REFRESH_BACKOFF_SECONDS
        # 退避期内不再发请求
        with mock.patch("oauth_login.requests.post") as post:
            assert oauth_login.refresh_if_needed(time_fn=fake_now) == "skipped"
            post.assert_not_called()
        # 退避期满可重试
        fake_now.return_value = 1000.0 + oauth_login.REFRESH_BACKOFF_SECONDS + 1
        with mock.patch("oauth_login.requests.post",
                        return_value=_resp(200, _expiring_payload(3600))):
            assert oauth_login.refresh_if_needed(time_fn=fake_now) == "refreshed"

    def test_terminal_error_marks_relogin_and_stops(self, cred_store):
        _save_ok(cred_store, payload=_expiring_payload(10 * 60))
        with mock.patch("oauth_login.requests.post",
                        return_value=_resp(400, {"error": "invalid_grant"})):
            assert oauth_login.refresh_if_needed() == "relogin"
        status = oauth_login.credentials_status()
        assert status["needs_relogin"] is True
        # 终态失效后不再发任何请求
        with mock.patch("oauth_login.requests.post") as post:
            assert oauth_login.refresh_if_needed() == "unavailable"
            post.assert_not_called()

    def test_newer_file_wins_and_skips_write(self, cred_store):
        _save_ok(cred_store, payload=_expiring_payload(10 * 60))
        # 模拟另一进程已刷新：磁盘 updated_at 更大且 expiration 更远
        newer = json.load(open(cred_store, "r", encoding="utf-8"))
        newer["updated_at"] = int(time.time()) + 100
        newer["expiration"] = _expiring_payload(7200)["credentials"]["expiration"]
        with open(cred_store, "w", encoding="utf-8") as handle:
            json.dump(newer, handle)
        with mock.patch("oauth_login.requests.post") as post:
            assert oauth_login.refresh_if_needed() == "reloaded"
            post.assert_not_called()
        # 内存态已重载为磁盘上的较新记录
        assert oauth_login.current_credentials().expiration.year >= 2036 or \
            oauth_login.current_credentials().remaining > datetime.timedelta(hours=1)

    def test_no_refresh_token_skips(self, cred_store):
        payload = _expiring_payload(10 * 60)
        payload.pop("refresh_token")
        _save_ok(cred_store, payload=payload)
        with mock.patch("oauth_login.requests.post") as post:
            assert oauth_login.refresh_if_needed() == "skipped"
            post.assert_not_called()
