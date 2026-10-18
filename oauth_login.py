# -*- coding: utf-8 -*-
"""OAuth2 PKCE + DPoP 浏览器登录协议层（对齐 codearts2api Go 参考实现）。

- PKCE：64 字节随机数 → base64url 无填充 verifier；challenge = base64url(sha256(verifier))。
- DPoP（RFC 9449）：ES256 + P-256，htu 为完整 token URL（无 query、无尾斜杠），低 S 归一化。
- ticket 兜底轮询不签名，仅带 plugin-name / plugin-version 头（此时尚未取得 STS 凭据）。
- cryptography 导入失败时抛出带安装提示的错误（design D2/D12）。
"""
import base64
import copy
import dataclasses
import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import quote
import urllib.parse

import requests

from oauth_protocol import (
    CLIENT_ID, PLUGIN_NAME, PLUGIN_VERSION, PORTAL_HOST,
    b64url as _b64url, build_authorize_url, generate_pkce,
    new_dpop_private_jwk,
)
from oauth_sessions import (
    OAuthSession, OAuthSessionStore, SESSION_MAX_CANDIDATES,
    SESSION_MAX_PER_ADMIN, SESSION_MAX_TOTAL, SESSION_TTL_SECONDS,
    SessionLimitError, SessionUnavailableError,
)

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
except ImportError as exc:  # pragma: no cover - 环境缺依赖时的可读提示
    raise ImportError(
        "缺少 cryptography 依赖，请先执行: pip install -r requirements.txt"
    ) from exc

# 对齐 codearts2api DefaultLoginConfig（逆向自 huaweicloud.authentication 扩展）
# CLIENT_ID / PORTAL_HOST / PLUGIN_NAME / PLUGIN_VERSION 见 oauth_protocol
SNAP_MANAGER = "https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager"
STS_HOST = "https://sts.cn-north-4.myhuaweicloud.com"
REDIRECT_PATH = "/oauth/callback"

TOKEN_URL = STS_HOST + "/v1/oauth2/tokens"
TICKET_PATH = "/v1/login/ticket"
CALLER_IDENTITY_URL = STS_HOST + "/v5/caller-identity"
CURRENT_USER_URL = (
    "https://snap-access.cn-north-4.myhuaweicloud.com" + "/v1/current/user"
)

# P-256 曲线阶（低 S 归一化与 d 范围校验用）
_P256_N = ec.SECP256R1().group_order

_TIMEOUT = 15  # 短请求统一超时（秒）


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


class RefreshTokenExpiredError(Exception):
    """refresh_token 终态失效，必须重新登录；与可重试错误严格分离。"""


# ---------------------------------------------------------------------------
# DPoP 密钥与证明
# ---------------------------------------------------------------------------

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


def sign_headers(method: str, url: str, body: bytes = b"",
                 snap: "Credentials | None" = None,
                 extra_headers: dict | None = None) -> dict:
    """构造华为云 SDK-HMAC-SHA256 签名头（canonical URI 带尾斜杠）。

    签名材料取 snap（缺省用当前 STS 快照）；始终附加 x-sdk-content-sha256，
    security_token 非空时附加 x-security-token，extra_headers 一并参与签名。
    """
    if snap is None:
        snap = current_credentials()
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    canonical_uri = path if path.endswith("/") else path + "/"
    params = sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    quote = lambda value: urllib.parse.quote(str(value), safe="-_.~")
    canonical_query = "&".join(f"{quote(k)}={quote(v)}" for k, v in params)

    headers = {}
    for key, value in (extra_headers or {}).items():
        headers[key.lower()] = str(value)
    headers["host"] = parsed.netloc
    headers.setdefault("content-type", "application/json")
    sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers["x-sdk-date"] = sdk_date
    headers["x-sdk-content-sha256"] = content_sha256(body)
    if snap.security_token:
        headers["x-security-token"] = snap.security_token

    signed_names = sorted(headers)
    canonical_headers = "".join(
        f"{name}:{headers[name].strip()}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    payload_hash = headers["x-sdk-content-sha256"]  # 同值复用，避免二次哈希
    canonical_request = (
        f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"SDK-HMAC-SHA256\n{sdk_date}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signature = hmac.new(
        snap.secret_access_key.encode(), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={snap.access_key_id}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


# ---------------------------------------------------------------------------
# 身份补全（design D13：异常分支只记类型名与状态码）
# ---------------------------------------------------------------------------

def _signed_get(url: str, snap: "Credentials") -> dict:
    """用 STS 快照签名 GET；失败抛 OSError（消息只含类型名/状态码）。"""
    headers = sign_headers(
        "GET", url, b"", snap=snap, extra_headers={"x-language": "zh-cn"}
    )
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise OSError(f"{type(exc).__name__}: identity request failed") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise OSError(f"identity response parse failed: http={resp.status_code}") from exc
    if resp.status_code >= 400:
        raise OSError(f"identity request failed: http={resp.status_code}")
    if not isinstance(data, dict):
        raise OSError(f"identity response not object: http={resp.status_code}")
    return data


def _sub_from_refresh_token(refresh_token: str) -> str:
    """从 JWT 形式的 refresh_token 取 sub 兜底 user_id（仅展示用）。"""
    parts = refresh_token.split(".")
    if len(parts) < 2:
        return ""
    try:
        claims = json.loads(_b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError):
        return ""
    return str(claims.get("sub") or "") if isinstance(claims, dict) else ""


def lookup_identity(snap: "Credentials") -> tuple[str, str, str]:
    """补全 (user_id, user_name, domain_id)；返回值用于展示，全空表示查不到。

    顺序：caller-identity（principal_urn 尾段取 name）→ current/user →
    refresh_token JWT 的 sub（仅 user_id）。每个失败分支只打印类型名/状态码。
    """
    try:
        data = _signed_get(CALLER_IDENTITY_URL, snap)
        name = ""
        urn = str(data.get("principal_urn") or "")
        if ":user:" in urn:
            name = urn.split(":user:")[-1]
        user_id = str(data.get("principal_id") or "")
        if user_id:
            return user_id, name, str(data.get("account_id") or "")
    except OSError as error:
        print(f"提示: 身份补全 caller-identity 失败: {error}")
    try:
        data = _signed_get(CURRENT_USER_URL, snap)
        user_id = str(data.get("user_id") or "")
        if user_id:
            return (user_id, str(data.get("user_name") or ""),
                    str(data.get("domain_id") or ""))
    except OSError as error:
        print(f"提示: 身份补全 current/user 失败: {error}")
    return _sub_from_refresh_token(snap.refresh_token), "", ""


# ---------------------------------------------------------------------------
# 凭据存储（design D3/D5）
# ---------------------------------------------------------------------------

REQUIRED_CRED_FIELDS = (
    "access_key_id", "secret_access_key", "security_token",
    "expiration", "dpop_private_key",
)

_cred_path = ""                  # oauth-auth.json 绝对路径（init 注入）
_cred_state: dict | None = None  # 内存态：完整凭据 dict（含 updated_at）
_cred_needs_relogin = False      # refresh_token 终态失效标记
_cred_initialized = False
_cred_lock = threading.RLock()   # 内存态读写 + 文件原子写互斥


class CredentialsUnavailable(Exception):
    """无可用 OAuth 凭据（未登录 / 需重新登录）。"""


@dataclasses.dataclass(frozen=True)
class Credentials:
    """不可变凭据快照：调用方在整个请求生命周期内持有同一份，不受后台刷新影响。"""

    access_key_id: str
    secret_access_key: str
    security_token: str
    expiration: datetime.datetime  # UTC 带时区
    refresh_token: str
    code_verifier: str
    client_id: str
    dpop_private_key: dict        # 构造时深拷贝，防外部篡改内存态
    user_id: str
    user_name: str
    domain_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "dpop_private_key", copy.deepcopy(self.dpop_private_key))

    @property
    def remaining(self) -> datetime.timedelta:
        return self.expiration - _utcnow()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_expiration(value) -> datetime.datetime | None:
    """解析 RFC3339 UTC 字符串为带时区 datetime；非法返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _normalize_cred_file(raw) -> dict | None:
    """把磁盘 JSON 归一为内存态 dict；结构非法返回 None（调用方降级告警）。

    内存态字段与文件字段一致，expiration 解析失败按未登录处理。
    """
    if not isinstance(raw, dict):
        return None
    for field in REQUIRED_CRED_FIELDS:
        if field == "dpop_private_key":
            continue  # dict 类型单独校验
        value = raw.get(field)
        if not isinstance(value, str) or not value:
            return None
    if not isinstance(raw.get("dpop_private_key"), dict):
        return None
    try:
        keypair_from_private_jwk(raw["dpop_private_key"])  # JWK 合法性校验
    except ValueError:
        return None
    if _parse_expiration(raw.get("expiration")) is None:
        return None
    return dict(raw)


def init(path: str) -> None:
    """加载凭据文件到内存；不存在视为未登录，损坏降级并告警（不退出进程）。"""
    global _cred_path, _cred_state, _cred_needs_relogin, _cred_initialized
    with _cred_lock:
        _cred_path = path
        _cred_state = None
        _cred_needs_relogin = False
        _cred_initialized = True
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            print("警告: oauth-auth.json 读取失败，已按未登录处理，请在管理页重新登录")
            return
        state = _normalize_cred_file(raw)
        if state is None:
            print("警告: oauth-auth.json 结构非法或凭据字段缺失，已按未登录处理")
            return
        _cred_state = state
    try:
        os.chmod(path, 0o600)  # 含凭据明文，兜底收紧权限
    except OSError:
        pass


def _require_initialized() -> None:
    if not _cred_initialized:
        raise RuntimeError("oauth_login 未初始化（init 未调用）")


def _write_locked(state: dict) -> None:
    """原子落盘（调用方已持锁）：唯一 tmp 名（pid+随机 hex）+ os.replace。

    先以 0600 建 tmp 再写入，杜绝 replace 前存在 0644 明文窗口
    （同 api_keys._write_locked / config_store 的写法）。
    """
    _require_initialized()
    tmp_path = f"{_cred_path}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    payload = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, _cred_path)
    except Exception:
        try:
            os.unlink(tmp_path)  # 清掉含明文的 tmp
        except OSError:
            pass
        raise


def save_credentials(token_response: dict, code_verifier: str,
                     dpop_private_jwk: dict) -> None:
    """登录成功后保存凭据并原子落盘（refresh_token 可缺失 → 不可自动续期）。"""
    creds = _normalize_token_response(token_response)
    expiration = _parse_expiration(creds["credentials"].get("expiration"))
    if expiration is None:
        raise ValueError("credential expiration missing or invalid")
    # 身份补全（proposal 三级链路）：token 响应通常不含身份字段，用刚取得的
    # STS 凭据查 caller-identity → current/user → refresh_token sub；失败不阻断
    # 登录（仅提示），显式回传的身份字段优先。
    user_id = str(token_response.get("user_id") or "")
    user_name = str(token_response.get("user_name") or "")
    domain_id = str(token_response.get("domain_id") or "")
    if not user_id:
        probe = Credentials(
            access_key_id=creds["credentials"]["access_key_id"],
            secret_access_key=creds["credentials"]["secret_access_key"],
            security_token=creds["credentials"]["security_token"],
            expiration=expiration,
            refresh_token=str(token_response.get("refresh_token") or ""),
            code_verifier=code_verifier,
            client_id=CLIENT_ID,
            dpop_private_key=dict(dpop_private_jwk),
            user_id="",          # 正是待补全字段，空串即语义正确
            user_name="",
            domain_id="",
        )
        try:
            looked_id, looked_name, looked_domain = lookup_identity(probe)
        except OSError as error:
            print(f"提示: 身份补全失败: {error}")
            looked_id, looked_name, looked_domain = "", "", ""
        user_id = user_id or looked_id
        user_name = user_name or looked_name
        domain_id = domain_id or looked_domain
    state = {
        "access_key_id": creds["credentials"]["access_key_id"],
        "secret_access_key": creds["credentials"]["secret_access_key"],
        "security_token": creds["credentials"]["security_token"],
        "expiration": creds["credentials"].get("expiration", ""),
        "refresh_token": str(token_response.get("refresh_token") or ""),
        "code_verifier": code_verifier,
        "client_id": CLIENT_ID,
        "dpop_private_key": dict(dpop_private_jwk),
        "user_id": user_id,
        "user_name": user_name,
        "domain_id": domain_id,
        "updated_at": int(time.time()),
    }
    global _cred_state, _cred_needs_relogin
    with _cred_lock:
        _require_initialized()
        _write_locked(state)
        _cred_state = state
        _cred_needs_relogin = False


def current_credentials() -> Credentials:
    """返回不可变凭据快照；无凭据或需重新登录抛 CredentialsUnavailable。"""
    _require_initialized()
    with _cred_lock:
        if _cred_needs_relogin:
            raise CredentialsUnavailable("oauth needs relogin")
        state = _cred_state
        if state is None:
            raise CredentialsUnavailable("oauth not logged in")
        expiration = _parse_expiration(state.get("expiration"))
        if expiration is None:
            raise CredentialsUnavailable("oauth credential expiration invalid")
        return Credentials(
            access_key_id=state["access_key_id"],
            secret_access_key=state["secret_access_key"],
            security_token=state["security_token"],
            expiration=expiration,
            refresh_token=state.get("refresh_token", ""),
            code_verifier=state.get("code_verifier", ""),
            client_id=state.get("client_id", CLIENT_ID),
            dpop_private_key=state["dpop_private_key"],
            user_id=state.get("user_id", ""),
            user_name=state.get("user_name", ""),
            domain_id=state.get("domain_id", ""),
        )


def credentials_status() -> dict:
    """管理面状态视图：只含展示字段，绝不含任何凭据明文。"""
    _require_initialized()
    with _cred_lock:
        if _cred_state is None:
            return {"logged_in": False, "needs_relogin": False}
        state = dict(_cred_state)
        needs_relogin = _cred_needs_relogin
    expiration = _parse_expiration(state.get("expiration"))
    return {
        "logged_in": True,
        "needs_relogin": needs_relogin,
        "user_id": state.get("user_id", ""),
        "user_name": state.get("user_name", ""),
        "domain_id": state.get("domain_id", ""),
        "expiration": state.get("expiration", ""),
        "has_refresh_token": bool(state.get("refresh_token")),
        "remaining_seconds": int(expiration.timestamp() - _utcnow().timestamp())
        if expiration else None,
    }


def mark_needs_relogin() -> None:
    """置终态失效标记：停止续期重试，后续上游调用按未登录处理。"""
    global _cred_needs_relogin
    with _cred_lock:
        _require_initialized()
        _cred_needs_relogin = True


def logout() -> None:
    """删除凭据文件并清空内存态；文件不存在也视为成功。

    顺序约束：先删文件成功再清内存（与 save_credentials 的「先盘后内存」
    一致），unlink 失败时保持登录态不变，避免「磁盘残留、重启后复活」。
    """
    global _cred_state, _cred_needs_relogin
    with _cred_lock:
        _require_initialized()
        if _cred_path and os.path.exists(_cred_path):
            try:
                os.unlink(_cred_path)
            except OSError:
                print("警告: oauth-auth.json 删除失败，请手动删除后重新登录")
                raise
        _cred_state = None
        _cred_needs_relogin = False


# ---------------------------------------------------------------------------
# 自动续期（design D9）
# ---------------------------------------------------------------------------

REFRESH_THRESHOLD = datetime.timedelta(minutes=30)  # 剩余多少时触发刷新
REFRESH_BACKOFF_SECONDS = 300.0                     # 瞬时错误退避（秒）

_next_retry_at = 0.0  # 退避截止时间戳（time.time 域）；0 表示无退避


def _read_cred_file_state() -> dict | None:
    """读盘并归一化；读取/解析失败按无文件处理（并发进程写坏极小概率）。"""
    try:
        with open(_cred_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _normalize_cred_file(raw)


def refresh_if_needed(now: datetime.datetime | None = None,
                      time_fn=time.time) -> str:
    """按需刷新凭据；返回值供续期线程与启动路径记日志（不含敏感值）。

    返回：
      "fresh"       剩余 > 30 分钟，无需刷新
      "skipped"     无 refresh_token（ticket 通道登录）或退避期内
      "reloaded"    磁盘 updated_at 比内存新，已重载并放弃本次写入
      "refreshed"   刷新成功并原子写回
      "relogin"     refresh_token 终态失效，已置 needs_relogin
      "backoff"     瞬时错误（网络/5xx），进入 5 分钟退避
      "unavailable" 无凭据或需重新登录
    """
    global _next_retry_at, _cred_state
    _require_initialized()
    now = now or _utcnow()
    with _cred_lock:
        if _cred_needs_relogin or _cred_state is None:
            return "unavailable"
        if not _cred_state.get("refresh_token"):
            return "skipped"
        if time_fn() < _next_retry_at:
            return "skipped"
        expiration = _parse_expiration(_cred_state.get("expiration"))
        if expiration is None:
            return "unavailable"
        if expiration - now > REFRESH_THRESHOLD:
            return "fresh"
        # 刷新前重读磁盘：其他进程可能已刷新过，较旧者放弃写入（design D9）
        file_state = _read_cred_file_state()
        if file_state is not None \
                and file_state.get("updated_at", 0) > _cred_state.get("updated_at", 0):
            _cred_state = file_state
            return "reloaded"
        snap = Credentials(
            access_key_id=_cred_state["access_key_id"],
            secret_access_key=_cred_state["secret_access_key"],
            security_token=_cred_state["security_token"],
            expiration=expiration,
            refresh_token=_cred_state["refresh_token"],
            code_verifier=_cred_state.get("code_verifier", ""),
            client_id=_cred_state.get("client_id", CLIENT_ID),
            dpop_private_key=_cred_state["dpop_private_key"],
            user_id=_cred_state.get("user_id", ""),
            user_name=_cred_state.get("user_name", ""),
            domain_id=_cred_state.get("domain_id", ""),
        )
    try:
        token_response = refresh_token_request(
            refresh_token=snap.refresh_token,
            code_verifier=snap.code_verifier,
            client_id=snap.client_id,
            dpop_private_jwk=snap.dpop_private_key,
        )
    except RefreshTokenExpiredError:
        mark_needs_relogin()
        with _cred_lock:
            _next_retry_at = 0.0
        return "relogin"
    except (OSError, ValueError):
        # 200 + 响应缺凭据/缺 expiration（ValueError）同样按瞬时错误退避
        with _cred_lock:
            _next_retry_at = time_fn() + REFRESH_BACKOFF_SECONDS
        return "backoff"
    # 上游不回新 refresh_token 时保留旧值（华为可能一次性轮换）
    if not token_response.get("refresh_token"):
        token_response["refresh_token"] = snap.refresh_token
    # 透传旧身份：refresh 响应通常无 user_id，避免每次续期重复走身份补全网络请求
    token_response.setdefault("user_id", snap.user_id)
    token_response.setdefault("user_name", snap.user_name)
    token_response.setdefault("domain_id", snap.domain_id)
    save_credentials(token_response, code_verifier=snap.code_verifier,
                     dpop_private_jwk=snap.dpop_private_key)
    with _cred_lock:
        _next_retry_at = 0.0
    return "refreshed"
