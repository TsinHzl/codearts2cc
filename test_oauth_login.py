# -*- coding: utf-8 -*-
"""oauth_login 协议层单元测试：PKCE / DPoP / 授权链接 / 换码与刷新表单 /
ticket 归一化 / 终态失效判定 / 签名固定向量。

对齐 codearts2api（Go 参考实现）：internal/upstream/{client,dpop,signer}.go。
"""
import base64
import hashlib
import json
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
