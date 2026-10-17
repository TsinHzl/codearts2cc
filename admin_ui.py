# -*- coding: utf-8 -*-
"""
admin 管理界面（自建适配版）
============================
为 codearts2cc 单账号本地代理提供内嵌管理面：概览 / 模型 / 额度 / 配置
四个页签，口令鉴权（config.json 的 ADMIN_PASSWORD），配置编辑写回 config.json。

安全约束：
- ADMIN_PASSWORD 未设置时，/admin 返回 403、/api/admin/* 统一拒绝（管理面未启用）
- AK/SK 只读脱敏展示、禁止编辑、日志不回显完整值
- CODEARTS_SNAP_SESSION 脱敏展示、可经 secrets 回写端点编辑（写后热生效）
- config.json 仅按白名单键精准编辑（映射 / 缓存开关），经 config_store 原子写回
- admin 蓝图豁免全局 CORS（不设 Access-Control-Allow-Origin: *）

代码数据面（由 server.py configure() 注入）：
- 模型列表 / 详情 / 强制刷新
- 通道模型集合（AGENT_MODELS 引用）
- benefit 签到 / 状态 / 余额
- config.json 路径与上游端点常量
"""
import hmac
import json
import os
import threading
import time

import requests
from flask import Blueprint, Response, abort, jsonify, redirect, request, send_file, session, url_for

import config_store

# ---------------------------------------------------------------------------
# 积分用量（华为云工作台 package/overview）
# ---------------------------------------------------------------------------

_CREDIT_URL = "https://devcloud.cn-north-4.huaweicloud.com/chat/snap-manager/v1/package/overview"
_CREDIT_SESSION_COOKIE = "devclouddevuibjJ_SESSION_ID"
_CREDIT_TIMEOUT = 15

# ---------------------------------------------------------------------------
# 依赖注入接口（server.py 注册时填充；禁止延迟 import server）
# ---------------------------------------------------------------------------

admin_password = ""            # config.json 的 ADMIN_PASSWORD
model_list_getter = None       # () -> list[str]，返回 SUPPORTED_MODELS
model_details_getter = None    # () -> dict，返回 MODEL_DETAILS
agent_models_ref = None        # AGENT_MODELS 列表引用
refresh_models_fn = None       # _refresh_models() -> bool（强制刷新）
benefit_balance_fn = None      # benefit.balance() -> dict
benefit_status_fn = None       # benefit.claim_status() -> dict
benefit_claim_fn = None        # benefit.claim() -> dict
usage_log_query_fn = None      # usage_log.query(page, limit) -> dict
config_path = ""               # config.json 绝对路径
upstream_endpoint = ""         # 上游聊天补全端点
listen_host = "127.0.0.1"
listen_port = 51818
model_cache_path = ""          # models-cache.json 绝对路径

# 登录失败退避：进程内内存态（threaded=True，需线程锁）
_fail_lock = threading.Lock()
_fail_count = 0
_fail_until = 0.0
_MAX_FAILURES = 5
_BACKOFF_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 900.0   # 指数退避上限 15 分钟
_lockout_rounds = 0            # 连续触发退避的轮数（指数基数）

# 配置编辑白名单键（仅这两键可写；AK/SK 只读且禁止编辑）
EDITABLE_KEYS = ("CODEARTS_ANTHROPIC_MODEL_MAP", "CODEARTS_CACHE_SIM")
READONLY_SECRET_KEYS = ("CODEARTS_AK", "CODEARTS_SK", "CODEARTS_SNAP_SESSION")
# secrets 回写可写键：AK/SK 写入需重启才生效（server 启动时固化），维持只读；
# session 由 admin_ui 每次请求实时读 config_store，写入即热生效
SECRET_WRITABLE_KEYS = ("CODEARTS_SNAP_SESSION",)


def configure(*, password, model_list, model_details, agent_models,
              refresh_models, benefit_balance, benefit_status, benefit_claim,
              config_path, upstream, host, port, cache_path, usage_log_query=None):
    """由 server.py 启动时调用，注入数据面依赖。"""
    global admin_password
    global model_list_getter, model_details_getter, agent_models_ref
    global refresh_models_fn, benefit_balance_fn, benefit_status_fn, benefit_claim_fn
    global usage_log_query_fn
    global upstream_endpoint, listen_host, listen_port, model_cache_path
    admin_password = password
    model_list_getter = model_list
    model_details_getter = model_details
    agent_models_ref = agent_models
    refresh_models_fn = refresh_models
    benefit_balance_fn = benefit_balance
    benefit_status_fn = benefit_status
    benefit_claim_fn = benefit_claim
    usage_log_query_fn = usage_log_query
    # 形参与模块级全局同名，`global config_path` 会报 SyntaxError，经 globals() 注入
    globals()["config_path"] = config_path
    upstream_endpoint = upstream
    listen_host = host
    listen_port = port
    model_cache_path = cache_path


blueprint = Blueprint("admin_ui", __name__)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    """管理面是否启用：口令已配置即启用。"""
    return bool(admin_password)


def _logged_in() -> bool:
    """当前请求是否已建立管理会话。"""
    return bool(_enabled() and session.get("admin_ok"))


def _check_backoff() -> bool:
    """登录退避：连续多次失败后短暂拒绝登录尝试。"""
    with _fail_lock:
        if _fail_until > time.monotonic():
            return True
        return False


def _record_failure() -> None:
    """记录一次登录失败，连续失败超过阈值后指数退避（上限 15 分钟）。"""
    global _fail_count, _fail_until, _lockout_rounds
    with _fail_lock:
        _fail_count += 1
        if _fail_count >= _MAX_FAILURES:
            delay = min(_BACKOFF_SECONDS * 2 ** _lockout_rounds, _BACKOFF_MAX_SECONDS)
            _lockout_rounds += 1
            _fail_until = time.monotonic() + delay
            _fail_count = 0


def _record_success() -> None:
    global _fail_count, _fail_until, _lockout_rounds
    with _fail_lock:
        _fail_count = 0
        _fail_until = 0.0
        _lockout_rounds = 0


def _json_error(message: str, status: int) -> Response:
    return jsonify({"error": message}), status


@blueprint.before_request
def _admin_gate():
    """admin 蓝图全局门禁：未启用 403、未登录 401。"""
    path = request.path
    if path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin"):
        if not _enabled():
            return _json_error("管理界面未启用：请先在 config.json 设置 ADMIN_PASSWORD", 403)
        if path == "/api/admin/login" or path == "/admin" or path.startswith("/admin/"):
            return None  # 登录接口与页面壳/静态资源（无数据）放行，登录态由前端探测
        if not _logged_in():
            return _json_error("未登录", 401)


_ADMIN_UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin-ui")
_ADMIN_DIST_DIR = os.path.join(_ADMIN_UI_DIR, "dist")


def _serve_admin_asset(filename: str, content_type: str) -> Response:
    """服务 admin-ui/dist 产物（含 index.html 与 assets/ 子目录），目录外与缺失一律 404。"""
    safe = os.path.normpath(filename)
    if safe.startswith("..") or os.path.isabs(safe):
        abort(404)
    filepath = os.path.join(_ADMIN_DIST_DIR, safe)
    if not os.path.isfile(filepath):
        abort(404)
    resp = send_file(filepath, mimetype=content_type)
    # 缓存分级：assets/ 带 contenthash 长缓存 immutable；其余（index.html、未来 public/
    # 拷贝的无 hash 文件）一律 no-store，防内容更新后浏览器永不拉新
    if safe.startswith("assets/"):
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    # svg 以活动文档渲染时可执行内嵌脚本（顶层导航场景），禁其脚本收口 XSS 面
    if content_type == "image/svg+xml":
        resp.headers["Content-Security-Policy"] = "script-src 'none'"
    return resp


@blueprint.route("/admin")
def admin_page():
    """管理界面壳：统一重定向到 /admin/（对齐 Vite base 前缀 /admin/，产物内引用均为绝对路径）。"""
    return redirect(url_for("admin_ui.admin_page_slash"))


@blueprint.route("/admin/")
def admin_page_slash():
    """管理界面壳：服务 admin-ui 预构建产物 index.html。未启用时由 before_request 拦截返回 403。"""
    return _serve_admin_asset("index.html", "text/html; charset=utf-8")


@blueprint.route("/admin/<path:filename>")
def admin_asset(filename: str):
    """管理界面静态资源（Vite 产物在 assets/ 子目录：js/css/字体/图标/sourcemap）。
    未启用时由 before_request 拦截返回 403。"""
    if filename.endswith((".js", ".mjs")):
        ctype = "application/javascript"
    elif filename.endswith(".css"):
        ctype = "text/css"
    elif filename.endswith(".html"):
        ctype = "text/html; charset=utf-8"
    elif filename.endswith((".json", ".map")):
        ctype = "application/json"
    elif filename.endswith(".svg"):
        ctype = "image/svg+xml"
    elif filename.endswith(".woff2"):
        ctype = "font/woff2"
    elif filename.endswith(".woff"):
        ctype = "font/woff"
    else:
        ctype = "application/octet-stream"
    return _serve_admin_asset(filename, ctype)


@blueprint.route("/api/admin/login", methods=["POST"])
def admin_login():
    """口令登录：成功建会话，失败退避拦截 + 记录失败。"""
    if not _enabled():
        return _json_error("管理界面未启用", 403)
    if _check_backoff():
        return _json_error("尝试次数过多，请稍后再试", 429)
    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password") or "").encode("utf-8")
    if not hmac.compare_digest(supplied, admin_password.encode("utf-8")):
        _record_failure()
        return _json_error("口令错误", 401)
    _record_success()
    session["admin_ok"] = True
    return jsonify({"ok": True})


@blueprint.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_ok", None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 概览 / 模型 / 额度 / 配置
# ---------------------------------------------------------------------------

@blueprint.route("/api/admin/overview", methods=["GET"])
def admin_overview():
    agent = list(agent_models_ref or [])
    all_models = model_list_getter() if model_list_getter else []
    benefit_count = len([m for m in all_models if m not in agent])
    cache_sim = config_store.get("CODEARTS_CACHE_SIM", "1").strip() != "0"
    try:
        cache_mtime = os.path.getmtime(model_cache_path)
    except OSError:
        cache_mtime = 0
    return jsonify({
        "listen": f"{listen_host}:{listen_port}",
        "listen_host": listen_host,
        "public_exposed": listen_host not in ("127.0.0.1", "localhost", "::1"),
        "upstream": upstream_endpoint,
        "model_count": len(all_models),
        "agent_count": len(agent),
        "benefit_count": benefit_count,
        "cache_sim_enabled": cache_sim,
        "cache_updated_at": int(cache_mtime) if cache_mtime else 0,
    })


@blueprint.route("/api/admin/models", methods=["GET"])
def admin_models():
    agent = set(agent_models_ref or [])
    details = model_details_getter() if model_details_getter else {}
    models = model_list_getter() if model_list_getter else []
    groups = {"agent": [], "benefit": []}
    for model_id in models:
        item = details.get(model_id) or {"id": model_id, "name": model_id}
        channel = "agent" if model_id in agent else "benefit"
        groups[channel].append({
            "id": item.get("id") or model_id,
            "name": item.get("name") or model_id,
            "channel": channel,
            "context_window": item.get("context_window"),
            "max_tokens": item.get("max_tokens"),
            "supports_images": bool(item.get("supports_images", False)),
        })
    return jsonify(groups)


@blueprint.route("/api/admin/models/refresh", methods=["POST"])
def admin_refresh_models():
    ok = refresh_models_fn() if refresh_models_fn else False
    return jsonify({"ok": bool(ok)}), (200 if ok else 502)


@blueprint.route("/api/admin/usage", methods=["GET"])
def admin_usage():
    """使用日志查询：统计 + 明细分页（page 默认 1 / limit 默认 50 上限 200，时间倒序）。"""
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    page = max(1, page)
    limit = min(max(1, limit), 200)
    return jsonify(usage_log_query_fn(page=page, limit=limit))


@blueprint.route("/api/admin/benefit", methods=["GET"])
def admin_benefit():
    try:
        balance = benefit_balance_fn() if benefit_balance_fn else {}
        status = benefit_status_fn() if benefit_status_fn else {}
    except Exception as error:  # noqa: BLE001
        return _json_error(f"查询额度失败: {error}", 502)
    return jsonify({
        "balance": balance or {},
        "status": status or {},
        "total_quota": (balance or {}).get("total_quota") or 0,
        "total_balance": (balance or {}).get("total_balance") or 0,
        "used_amount": (balance or {}).get("used_amount") or 0,
        "last_claim_ms": (status or {}).get("create_time") or 0,
    })


@blueprint.route("/api/admin/benefit/claim", methods=["POST"])
def admin_claim():
    try:
        result = benefit_claim_fn() if benefit_claim_fn else {}
    except Exception as error:  # noqa: BLE001
        return _json_error(f"签到失败: {error}", 502)
    return jsonify({"ok": True, "status": result or {}})


# ---------------------------------------------------------------------------
# 配置（config.json 白名单键读写，经 config_store）
# ---------------------------------------------------------------------------

# 积分用量：把上游 package/overview 归一化为 {amount, used, remain}。
# 失效分支三分类见 proposal；异常信息只含状态码/错误类型，不含 Cookie 值。
def _fetch_credit_usage() -> dict:
    session_value = (config_store.get("CODEARTS_SNAP_SESSION") or "").strip()
    if not session_value:
        return {"error": "未配置 CODEARTS_SNAP_SESSION，请从华为云工作台 Cookie 中"
                         "复制 devclouddevuibjJ_SESSION_ID 的值到 config.json", "status": 400}
    try:
        resp = requests.get(
            _CREDIT_URL,
            cookies={_CREDIT_SESSION_COOKIE: session_value},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Referer": "https://devcloud.cn-north-4.huaweicloud.com/chat/workbench",
                "x-requested-with": "XMLHttpRequest",
                "x-codearts-doer-scenario-type": "web",
            },
            timeout=_CREDIT_TIMEOUT,
        )
    except requests.RequestException as error:
        # 仅保留异常类型名，杜绝 requests 异常堆栈携带 URL/Cookie 落日志
        return {"error": f"积分接口请求失败（网络或上游错误）: {type(error).__name__}", "status": 502}
    if resp.status_code != 200:
        return {"error": f"积分接口请求失败（网络或上游错误）: HTTP {resp.status_code}", "status": 502}
    body = resp.text.strip()
    if not body:
        return {"error": "积分会话已过期，请更新 CODEARTS_SNAP_SESSION", "status": 502}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"error": "积分会话已过期，请更新 CODEARTS_SNAP_SESSION", "status": 502}
    pkg = payload.get("all_credit_package") if isinstance(payload, dict) else None
    if not isinstance(pkg, dict):
        return {"error": "积分接口响应结构异常，可能上游已变更", "status": 502}
    amount = pkg.get("package_credit_amount")
    used = pkg.get("package_credit_used")
    remain = pkg.get("package_credit_remain")
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in (amount, used, remain)
    ):
        return {"error": "积分接口响应结构异常，可能上游已变更", "status": 502}
    return {"amount": amount, "used": used, "remain": remain}


@blueprint.route("/api/admin/credit", methods=["GET"])
def admin_credit():
    """积分用量查询：转发华为云 package/overview，归一化为 {amount, used, remain}。"""
    result = _fetch_credit_usage()
    if "error" in result:
        return _json_error(result["error"], result["status"])
    return jsonify(result)


def _mask(value: str) -> str:
    """脱敏：仅保留首尾各 2 字符。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


@blueprint.route("/api/admin/config", methods=["GET"])
def admin_get_config():
    env_map = config_store.as_dict()
    editable = {key: env_map.get(key, "") for key in EDITABLE_KEYS}
    secrets = {key: _mask(env_map.get(key, "")) for key in READONLY_SECRET_KEYS}
    return jsonify({"editable": editable, "secrets": secrets})


@blueprint.route("/api/admin/config", methods=["PUT"])
def admin_set_config():
    data = request.get_json(silent=True) or {}
    edits = data.get("editable") or {}
    if not isinstance(edits, dict):
        return _json_error("editable 必须为对象", 400)
    # 只接受白名单键；拒绝任何密钥键（即使拼写类似）
    for key in edits:
        if key not in EDITABLE_KEYS:
            return _json_error(f"不支持编辑配置键: {key}", 400)
    for key, value in edits.items():
        # 值必须是单行可打印字符串：杜绝换行注入伪造配置键（如 ADMIN_PASSWORD=）
        if (not isinstance(value, str) or not value.isprintable()
                or "\n" in value or "\r" in value):
            return _json_error(f"配置键 {key} 的值必须为单行可打印字符串", 400)
    config_store.set_many({key: value for key, value in edits.items()})
    return jsonify({"ok": True, "updated": dict(edits)})


@blueprint.route("/api/admin/config/secrets", methods=["PUT"])
def admin_set_secret():
    """secrets 回写通路：仅接受 SECRET_WRITABLE_KEYS 中的键写入新值。

    提交值等于掩码输出或纯 * 串时跳过更新（页面回显的是掩码，不覆盖真实值）；
    错误信息只含键名，不回显提交值本身。
    """
    data = request.get_json(silent=True) or {}
    updates = data.get("secrets") or {}
    if not isinstance(updates, dict):
        return _json_error("secrets 必须为对象", 400)
    for key in updates:
        if key not in SECRET_WRITABLE_KEYS:
            return _json_error(f"不支持编辑配置键: {key}", 400)
    for key, value in updates.items():
        if (not isinstance(value, str) or not value.isprintable()
                or "\n" in value or "\r" in value):
            return _json_error(f"配置键 {key} 的值必须为单行可打印字符串", 400)
    applied = {}
    for key, value in updates.items():
        if not value.strip():
            continue
        # 掩码回显值 / 纯 * 串 → 用户未改动，跳过
        if value == _mask(config_store.get(key)) or set(value) == {"*"}:
            continue
        applied[key] = value
    if applied:
        config_store.set_many(applied)
    return jsonify({"ok": True, "updated": sorted(applied)})


