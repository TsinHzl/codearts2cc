# -*- coding: utf-8 -*-
"""OAuth2 协议基础构件：PKCE、DPoP 私钥 JWK 与 Portal 授权链接。

独立于凭据存储与会话仓库，供 oauth_login / oauth_sessions 共用，
避免二者互相 import 形成循环依赖。
"""
import base64
import hashlib
import secrets
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric import ec

CLIENT_ID = "codearts-agent"
PORTAL_HOST = "https://codearts.huaweicloud.com/portal"
PLUGIN_NAME = "snap_AIIDE"
PLUGIN_VERSION = "5.2.0"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_pkce() -> tuple[str, str]:
    """生成 (verifier, challenge)：64 字节随机数的 base64url 无填充编码。"""
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_dpop_private_jwk() -> dict:
    """生成可持久化的 P-256 私钥 JWK（kty/crv/x/y/d，均为 32 字节 base64url）。"""
    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": b64url(nums.x.to_bytes(32, "big")),
        "y": b64url(nums.y.to_bytes(32, "big")),
        "d": b64url(key.private_numbers().private_value.to_bytes(32, "big")),
    }


def build_authorize_url(ticket_id: str, code_challenge: str, state: str, port: int) -> str:
    """构造 portal 授权链接（PKCE)；port 为本地回调端口（取自 LISTEN_PORT）。"""
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
