# -*- coding: utf-8 -*-
"""
OAuth 登录管理端点
==================
提供 /api/admin/oauth/* 的 start / poll / import-callback / logout / status，
供管理页「登录」页签驱动浏览器 OAuth 登录流程（design D8）。

安全约束：
- 本蓝图挂在 admin_ui 下，继承父蓝图的 before_request（口令会话门禁），
  不要重复挂载鉴权，也不要把本蓝图提升为 app 级独立蓝图
- 所有响应只返回状态与账号展示信息，绝不返回凭据字段
  （refresh_token / code_verifier / DPoP 私钥 / STS 明文）
- 会话操作（poll / import-callback）校验同一管理会话，跨会话 409
- 异常信息只含错误类型名与 HTTP 状态码
"""
import ipaddress
import logging
import secrets
import urllib.parse

from flask import Blueprint, Response, jsonify, request, session

from src.oauth import oauth_login


def _is_loopback_host(host: str) -> bool:
    """回调 URL 的主机名是否回环（与 server._is_loopback_host 同语义）。"""
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

logger = logging.getLogger(__name__)

blueprint = Blueprint("admin_oauth", __name__)

# 依赖注入（server.py 挂载时填充；禁止延迟 import server）
oauth_sessions = None          # oauth_login.OAuthSessionStore 实例
listen_port = 51818            # LISTEN_PORT（授权链接 port 参数与会话端口）


def configure(sessions, port: int) -> None:
    """由 server.py 调用：注入会话仓库与监听端口。"""
    global oauth_sessions, listen_port
    oauth_sessions = sessions
    listen_port = port


def _admin_session_id() -> str:
    """管理会话标识：Flask session 内持久随机 sid（首次生成，跨请求稳定）。

    与 admin_ok 一起存在于签名 cookie session 中，无需额外 CSRF token
    （design D6）：跨浏览器会话拿不到同一 sid，即被 409 拦下。
    """
    sid = session.get("sid")
    if not sid:
        sid = secrets.token_hex(16)
        session["sid"] = sid
    return sid


def _json_error(message: str, status: int):
    return jsonify({"error": message}), status


def _ticket_matches(received: str, expected: str) -> bool:
    """恒定时间比较 ticket_id；非 ASCII 输入按不匹配处理（避免 TypeError）。"""
    try:
        return secrets.compare_digest(
            received.encode("ascii"), expected.encode("ascii"))
    except UnicodeEncodeError:
        return False


# ---------------------------------------------------------------------------
# start：生成全套登录材料并返回授权链接
# ---------------------------------------------------------------------------

@blueprint.route("/api/admin/oauth/start", methods=["POST"])
def oauth_start():
    try:
        login = oauth_sessions.create(listen_port, _admin_session_id())
    except oauth_login.SessionLimitError:
        return _json_error("session limit reached", 409)
    return jsonify({
        "session_id": login.id,
        "auth_url": login.auth_url,
        # 提示字段：回调导入需要用户把最终回调 URL 粘贴回管理页
        "callback_hint": f"http://127.0.0.1:{listen_port}{oauth_login.REDIRECT_PATH}",
    })


# ---------------------------------------------------------------------------
# poll：短轮询，立即返回当前状态
# ---------------------------------------------------------------------------

@blueprint.route("/api/admin/oauth/poll", methods=["GET"])
def oauth_poll():
    session_id = request.args.get("session_id", "")
    if not session_id:
        return _json_error("session_id required", 400)
    try:
        login = oauth_sessions.require(session_id, _admin_session_id())
    except oauth_login.SessionUnavailableError:
        return _json_error("session not available", 409)

    # 已结束：直接回传结果状态（err 为可读原因，不含敏感值）
    if login.done:
        return jsonify({"status": "err" if login.err else "ok",
                        "error": login.err or ""})

    # ticket 兜底轮询（每次 poll 最多 1 次；门控未开时只报 pending，design D6）
    if login.ticket_fallback:
        try:
            token_response = oauth_login.poll_ticket(login.ticket_id, login.secret)
        except (OSError, ValueError) as error:
            # 瞬时失败：保持 pending，前端下个周期再来；不记录敏感值
            return jsonify({"status": "pending",
                            "detail": type(error).__name__})
        try:
            oauth_login.save_credentials(
                token_response, code_verifier=login.verifier,
                dpop_private_jwk=login.dpop_private_key,
            )
        except (OSError, ValueError) as error:
            oauth_sessions.complete(session_id, err=type(error).__name__)
            return jsonify({"status": "err", "error": type(error).__name__})
        oauth_sessions.complete(session_id)
        return jsonify({"status": "ok", "error": ""})

    return jsonify({"status": "pending", "detail": ""})


# ---------------------------------------------------------------------------
# import-callback：手工导入浏览器回调 URL（浏览器不在代理主机时）
# ---------------------------------------------------------------------------

@blueprint.route("/api/admin/oauth/import-callback", methods=["POST"])
def oauth_import_callback():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    session_id = str(data.get("session_id") or "")
    url = str(data.get("url") or "")
    if not session_id or not url:
        return _json_error("session_id and url required", 400)
    try:
        login = oauth_sessions.require(session_id, _admin_session_id())
    except oauth_login.SessionUnavailableError:
        return _json_error("session not available", 409)

    # URL 白名单校验（design D8）：http、无 userinfo、path /oauth/callback、
    # 回环主机、端口等于会话端口；redirect 参数须 https + PortalHost +
    # ticket_id 与会话一致
    try:
        target = urllib.parse.urlparse(url)
        target_port = target.port  # 非数字端口在此抛 ValueError
    except ValueError:
        return _json_error("invalid callback url", 400)
    if (target.scheme != "http" or target.username is not None
            or target.password is not None
            or target.path != oauth_login.REDIRECT_PATH
            or not _is_loopback_host(target.hostname or "")
            or target_port != login.port):
        return _json_error("invalid callback url", 400)

    params = urllib.parse.parse_qs(target.query, keep_blank_values=True)
    code = (params.get("code") or [""])[0]
    secret = (params.get("secret") or [""])[0]
    redirect = (params.get("redirect") or [""])[0]

    # secret 阶段：换入新 secret 并开启 ticket 兜底（与 /oauth/callback 行为一致）
    if secret and redirect:
        portal = urllib.parse.urlparse(oauth_login.PORTAL_HOST)
        try:
            redirect_target = urllib.parse.urlparse(redirect)
        except ValueError:
            return _json_error("invalid redirect parameter", 400)
        expected_path = portal.path.rstrip("/") + "/oauth/callback"
        redirect_ticket = (urllib.parse.parse_qs(redirect_target.query)
                           .get("ticket_id", [""]))[0]
        if (redirect_target.scheme != "https"
                or (redirect_target.hostname or "").lower()
                != (portal.hostname or "").lower()
                or redirect_target.path != expected_path
                or any(ch in redirect for ch in "\r\n\x00")
                or not redirect_ticket
                or not _ticket_matches(redirect_ticket, login.ticket_id)):
            return _json_error("invalid redirect parameter", 400)
        oauth_sessions.enable_ticket_fallback(login.id, secret)
        return jsonify({"status": "pending", "detail": ""})

    if not code:
        return _json_error("code required", 400)
    return _finish_exchange(login, code)


def _finish_exchange(login, code: str):
    """用会话材料换码并持久化（与 /oauth/callback code 阶段共用语义）。

    import-callback 场景会话已由 require() 校验归属，直接用该会话换码；
    错误体只含错误类型名（design D8 错误契约），失败时结束会话并回传 err。
    """
    try:
        token_response = oauth_login.exchange_code(
            code, login.verifier, login.port, login.dpop_private_key,
        )
    except (OSError, ValueError, oauth_login.RefreshTokenExpiredError) as error:
        oauth_sessions.complete(login.id, err=type(error).__name__)
        return _json_error("code exchange failed", 502)
    try:
        oauth_login.save_credentials(
            token_response, code_verifier=login.verifier,
            dpop_private_jwk=login.dpop_private_key,
        )
    except (OSError, ValueError) as error:
        oauth_sessions.complete(login.id, err=type(error).__name__)
        return _json_error("code exchange failed", 502)
    oauth_sessions.complete(login.id)
    return jsonify({"status": "ok", "error": ""})


# ---------------------------------------------------------------------------
# logout / status
# ---------------------------------------------------------------------------

@blueprint.route("/api/admin/oauth/logout", methods=["POST"])
def oauth_logout():
    try:
        oauth_login.logout()
    except OSError as error:
        return _json_error(f"logout failed: {type(error).__name__}", 502)
    return jsonify({"ok": True})


@blueprint.route("/api/admin/oauth/status", methods=["GET"])
def oauth_status():
    """登录状态白名单字段（design D8：绝不返回凭据字段）。"""
    try:
        return jsonify(oauth_login.credentials_status())
    except RuntimeError:
        return _json_error("credential store not initialized", 503)
