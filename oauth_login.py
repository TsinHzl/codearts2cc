# -*- coding: utf-8 -*-
"""OAuth2 PKCE + DPoP 浏览器登录协议层（对齐 codearts2api Go 参考实现）。

- PKCE：64 字节随机数 → base64url 无填充 verifier；challenge = base64url(sha256(verifier))。
- DPoP（RFC 9449）：ES256 + P-256，htu 为完整 token URL（无 query、无尾斜杠），低 S 归一化。
- ticket 兜底轮询不签名，仅带 plugin-name / plugin-version 头（此时尚未取得 STS 凭据）。
- cryptography 导入失败时抛出带安装提示的错误（design D2/D12）。
"""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import quote, urlencode

import requests

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
except ImportError as exc:  # pragma: no cover - 环境缺依赖时的可读提示
    raise ImportError(
        "缺少 cryptography 依赖，请先执行: pip install -r requirements.txt"
    ) from exc

# 对齐 codearts2api DefaultLoginConfig（逆向自 huaweicloud.authentication 扩展）
CLIENT_ID = "codearts-agent"
PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
SNAP_MANAGER = "https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager"
STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
REDIRECT_PATH = "/oauth/callback"
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"

TOKEN_URL = STS_HOST + "/v1/oauth2/tokens"
TICKET_PATH = "/v1/login/ticket"
CALLER_IDENTITY_URL = STS_HOST + "/v5/caller-identity"
CURRENT_USER_URL = (
    "https://snap-access.cn-north-4.myhuaweicloud.com" + "/v1/current/user"
)

# P-256 曲线阶（低 S 归一化与 d 范围校验用）
_P256_N = ec.SECP256R1().group_order

_TIMEOUT = 15  # 短请求统一超时（秒）


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


class RefreshTokenExpiredError(Exception):
    """refresh_token 终态失效，必须重新登录；与可重试错误严格分离。"""


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def generate_pkce() -> tuple[str, str]:
    """生成 (verifier, challenge)：64 字节随机数的 base64url 无填充编码。"""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


# ---------------------------------------------------------------------------
# DPoP 密钥与证明
# ---------------------------------------------------------------------------

def new_dpop_private_jwk() -> dict:
    """生成可持久化的 P-256 私钥 JWK（kty/crv/x/y/d，均为 32 字节 base64url）。"""
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(nums.x.to_bytes(32, "big")),
        "y": _b64url(nums.y.to_bytes(32, "big")),
        "d": _b64url(key.private_numbers().private_value.to_bytes(32, "big")),
    }


def keypair_from_private_jwk(jwk: dict) -> tuple[ec.EllipticCurvePrivateKey, dict]:
    """由私钥 JWK 还原密钥对与公钥 JWK；非法材料抛 ValueError（不含字段值）。"""
    try:
        if jwk["kty"] != "EC" or jwk["crv"] != "P-256":
            raise ValueError("invalid dpop jwk curve")
        x = int.from_bytes(_b64url_decode(jwk["x"]), "big")
        y = int.from_bytes(_b64url_decode(jwk["y"]), "big")
        d = int.from_bytes(_b64url_decode(jwk["d"]), "big")
        if not 0 < d < _P256_N:
            raise ValueError("invalid dpop jwk key material")
        key = ec.derive_private_key(d, ec.SECP256R1())
        pub = key.public_key().public_numbers()
        if pub.x != x or pub.y != y:
            raise ValueError("dpop jwk public/private key mismatch")
    except (KeyError, TypeError) as exc:
        # 非法材料（非 dict、缺字段、字段非 str）统一收口为契约异常，且不携带字段值
        raise ValueError(f"invalid dpop jwk: {type(exc).__name__}") from exc
    public_jwk = {"kty": "EC", "crv": "P-256", "x": jwk["x"], "y": jwk["y"]}
    return key, public_jwk


def sign_dpop_proof(jwk: dict) -> str:
    """生成 DPoP 证明 JWT（htm=POST、htu=token 端点、低 S 归一化）。"""
    key, public_jwk = keypair_from_private_jwk(jwk)
    header = {"alg": "ES256", "typ": "dpop+jwt", "jwk": public_jwk}
    payload = {
        "htm": "POST",
        "htu": TOKEN_URL,  # 完整 URL，无 query、无尾斜杠
        "iat": int(time.time()),
        "jti": secrets.token_hex(16),  # 16 字节 → 32 位 hex
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    der = key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    if s > _P256_N // 2:  # 低 S 归一化（服务端兼容性）
        s = _P256_N - s
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return signing_input + "." + _b64url(sig)


# ---------------------------------------------------------------------------
# 授权链接
# ---------------------------------------------------------------------------

def build_authorize_url(ticket_id: str, code_challenge: str, state: str, port: int) -> str:
    """构造 portal 授权链接（PKCE）；port 为本地回调端口（取自 LISTEN_PORT）。"""
    params = {
        "theme": "2",
        "locale": "zh-cn",
        "uri_scheme": CLIENT_ID,
        "client_id": CLIENT_ID,
        "port": str(port),
        "code_challenge": code_challenge,
        "code_challenge_method": "SHA-256",
        "state": state,
        "ticket_id": ticket_id,
        "plugin-name": PLUGIN_NAME,
        "plugin-version": PLUGIN_VERSION,
    }
    return PORTAL_HOST + "/authorize?" + urlencode(params)


# ---------------------------------------------------------------------------
# token 端点（换码 / 刷新）
# ---------------------------------------------------------------------------

def _normalize_token_response(data: dict, grant_type: str = "") -> dict:
    """归一化 oauth2/tokens 与 ticket（legacy credential）两种响应结构。

    缺失凭据时：refresh 通道先判终态失效（HTTP 200 + error body 的上游怪癖），
    再抛 ValueError；错误信息不含任何字段值。
    """
    creds = data.get("credentials") or {}
    if not creds.get("security_token"):
        legacy = data.get("credential") or {}
        if legacy.get("securitytoken"):
            creds = {
                "access_key_id": legacy.get("access", ""),
                "secret_access_key": legacy.get("secret", ""),
                "security_token": legacy.get("securitytoken", ""),
                "expiration": legacy.get("expires_at", ""),
            }
    if not (creds.get("access_key_id") and creds.get("secret_access_key")
            and creds.get("security_token")):
        if grant_type == "refresh_token" and _is_refresh_terminal(data):
            raise RefreshTokenExpiredError("refresh token expired: http=200")
        raise ValueError("token response missing credentials")
    out = dict(data)
    out["credentials"] = creds
    return out


def _is_refresh_terminal(data: dict) -> bool:
    """refresh 通道终态失效判定（对齐 codearts2api requestToken）。"""
    if data.get("error") == "invalid_grant":
        return True
    code = str(data.get("error_code", ""))
    if ("ExpiredRefreshToken" in code or "InvalidDPoPHeader" in code
            or "invalid client id" in code):
        return True
    return "has been used" in str(data.get("error_msg", "")).lower()


def exchange_code(code: str, code_verifier: str, port: int, dpop_private_jwk: dict) -> dict:
    """授权码换取 STS 临时凭据（POST {sts}/v1/oauth2/tokens + DPoP 头）。"""
    form = {
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": f"http://127.0.0.1:{port}{REDIRECT_PATH}",
    }
    return _request_token(form, dpop_private_jwk)


def refresh_token_request(refresh_token: str, code_verifier: str,
                           client_id: str, dpop_private_jwk: dict) -> dict:
    """用 refresh_token 换新凭据；必须复用签发时的 DPoP 私钥与 client_id。"""
    form = {
        "client_id": client_id,
        "code_verifier": code_verifier,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    return _request_token(form, dpop_private_jwk)


def _request_token(form: dict, dpop_private_jwk: dict) -> dict:
    proof = sign_dpop_proof(dpop_private_jwk)
    try:
        resp = requests.post(
            TOKEN_URL, data=form, timeout=_TIMEOUT,
            headers={"Content-Type": "application/x-www-form-urlencoded", "DPoP": proof},
        )
    except requests.RequestException as exc:
        raise OSError(f"{type(exc).__name__}: exchange failed") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise OSError(f"token response parse failed: http={resp.status_code}") from exc
    if resp.status_code >= 400:
        if form.get("grant_type") == "refresh_token" and _is_refresh_terminal(data):
            raise RefreshTokenExpiredError(
                f"refresh token expired: http={resp.status_code}")
        raise OSError(f"token request failed: http={resp.status_code}")
    return _normalize_token_response(data, grant_type=form.get("grant_type", ""))


# ---------------------------------------------------------------------------
# ticket 兜底（不签名）
# ---------------------------------------------------------------------------

def poll_ticket(ticket_id: str, secret: str) -> dict:
    """轮询登录 ticket（GET，不签名，仅 plugin 头）。"""
    path = f"{TICKET_PATH}?ticket_id={quote(ticket_id, safe='')}&secret={quote(secret, safe='')}"
    try:
        resp = requests.get(
            SNAP_MANAGER + path, timeout=_TIMEOUT,
            headers={"plugin-name": PLUGIN_NAME, "plugin-version": PLUGIN_VERSION},
        )
    except requests.RequestException as exc:
        raise OSError(f"{type(exc).__name__}: ticket poll failed") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise OSError(f"ticket response parse failed: http={resp.status_code}") from exc
    if resp.status_code >= 400:
        raise OSError(f"ticket poll failed: http={resp.status_code}")
    return _normalize_token_response(data)


# ---------------------------------------------------------------------------
# 签名辅助
# ---------------------------------------------------------------------------

def content_sha256(body: bytes) -> str:
    """SDK-HMAC-SHA256 的 X-Sdk-Content-Sha256 取值：sha256(body) 小写 hex。"""
    return hashlib.sha256(body).hexdigest()
