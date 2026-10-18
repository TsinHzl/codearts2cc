# -*- coding: utf-8 -*-
"""
admin 管理界面（自建适配版）
============================
为 codearts2cc 单账号本地代理提供内嵌管理面：概览 / 模型 / 额度 / 使用日志 /
配置 / API Key 六个页签，口令鉴权（config.json 的 ADMIN_PASSWORD），配置编辑写回
config.json。API Key 端点已拆至 admin_api_keys.py，其蓝图在此嵌套注册并挂载同一门禁。

安全约束：
- ADMIN_PASSWORD 未设置时，/admin 返回 403、/api/admin/* 统一拒绝（管理面未启用）
- AK/SK 只读脱敏展示、禁止编辑、日志不回显完整值
- CODEARTS_SNAP_SESSION 脱敏展示、可经 secrets 回写端点编辑（写后热生效）
- config.json 仅按白名单键精准编辑（映射 / 缓存开关 / 账号名 / 单价表 / Credits 系数 / 思考文本化 / API Key 强制鉴权），经 config_store 原子写回
- admin 蓝图豁免全局 CORS（不设 Access-Control-Allow-Origin: *）

代码数据面（由 server.py configure() 注入）：
- 模型列表 / 详情 / 强制刷新
- 通道模型集合（AGENT_MODELS 引用）
- benefit 签到 / 状态 / 余额
- config.json 路径与上游端点常量
"""
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime

import requests
from flask import Blueprint, Response, abort, jsonify, redirect, request, send_file, session, url_for

import admin_api_keys
import config_store
import pricing

logger = logging.getLogger(__name__)

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

# 配置编辑白名单键（AK/SK 只读且禁止编辑）
EDITABLE_KEYS = (
    "CODEARTS_ANTHROPIC_MODEL_MAP",
    "CODEARTS_CACHE_SIM",
    "CODEARTS_ACCOUNT_NAME",
    "CODEARTS_PRICING",
    "CODEARTS_CREDITS_RATE",
    "CODEARTS_THINKING_AS_TEXT",
    "CODEARTS_REQUIRE_API_KEY",
)
# 新增配置键的页面默认值（仅键缺失时回显）
_EDITABLE_DEFAULTS = {
    "CODEARTS_ACCOUNT_NAME": "主账号",
    "CODEARTS_PRICING": "{}",
    "CODEARTS_CREDITS_RATE": "1.0",
    "CODEARTS_THINKING_AS_TEXT": "1",
    "CODEARTS_REQUIRE_API_KEY": "1",
}
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


# API Key 端点使用独立蓝图（admin_api_keys.py）。Flask 对嵌套蓝图路由会沿
# request.blueprints 依次执行父蓝图的 before_request，_admin_gate 已覆盖
# /api/admin/api-keys，此处不得重复挂载，否则门禁每请求执行两次。
blueprint.register_blueprint(admin_api_keys.blueprint)


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
    """使用日志查询：区间过滤 + 分页 + 计价 + 可选每日聚合。"""
    try:
        start = _parse_date_arg("start")
        end = _parse_date_arg("end")
    except ValueError as error:
        return _json_error(str(error), 400)
    if start and end and start > end:
        return _json_error("start 不能晚于 end", 400)
    page, limit = _page_args()
    daily = _daily_flag()

    result = usage_log_query_fn(page=page, limit=limit, start=start, end=end, daily=daily)
    table, warning = pricing.load_table()
    if warning:
        logger.warning("%s", warning)
    rate = pricing.load_rate()
    account = (config_store.get("CODEARTS_ACCOUNT_NAME", "") or "").strip() or "主账号"

    entries, totals = _price_entries(result.get("entries") or [], table, rate, account)

    daily_rows = result.get("daily")
    if daily_rows is not None:
        daily_rows = _price_daily(daily_rows, result.get("daily_models") or [], table, rate)

    return jsonify({
        "summary": result.get("summary") or {},
        "by_model": result.get("by_model") or {},
        "entries": entries,
        "page": {
            "page": result.get("page", page),
            "limit": result.get("limit", limit),
            "total": result.get("total", 0),
        },
        "page_totals": totals,
        "daily": daily_rows,
        "account": account,
        "start": start,
        "end": end,
    })


_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _page_args() -> tuple:
    """解析分页参数：page 最小 1；limit 默认 100、上限 200。"""
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    return max(1, page), min(max(1, limit), 200)


def _parse_date_arg(name: str) -> str:
    """解析 YYYY-MM-DD 查询参数；缺失返回空串，格式或日期非法抛 ValueError。"""
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return ""
    if not _DATE_PATTERN.match(raw):
        raise ValueError(f"{name} 必须为 YYYY-MM-DD 格式")
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{name} 不是有效日期") from None
    return raw


def _daily_flag() -> bool:
    """daily 开关：1 / true（大小写不敏感、容忍首尾空格）为开启。"""
    return (request.args.get("daily") or "").strip().lower() in ("1", "true")


def _price_entries(entries: list, table: dict, rate: float, account: str) -> tuple:
    """为明细注入账号与计价字段，并汇总本页金额（只计有单价记录）。"""
    priced_entries = []
    totals = {"requests": 0, "priced_requests": 0,
              "cost": None, "credits": None, "saved": None}
    cost_sum = credits_sum = saved_sum = 0.0
    for entry in entries:
        priced = pricing.compute(entry, table, rate)
        if priced is None:
            priced_entries.append({**entry, "account": account,
                                   "cost": None, "credits": None, "saved": None})
            continue
        priced_entries.append({**entry, "account": account, **priced})
        totals["priced_requests"] += 1
        cost_sum += priced["cost"]
        credits_sum += priced["credits"]
        saved_sum += priced["saved"]
    totals["requests"] = len(priced_entries)
    if totals["priced_requests"]:
        totals["cost"] = cost_sum
        totals["credits"] = credits_sum
        totals["saved"] = saved_sum
    return priced_entries, totals


def _price_daily(rows: list, model_rows: list, table: dict, rate: float) -> list:
    """为每日聚合补齐费用字段。

    usage_log 已按 (日期, 模型) 聚合同类 token，费用对桶线性可加，因此只对桶
    计价，不再回扫全量明细。某日全部记录未计价时金额为 None。桶内脏 token
    已由 usage_log 折算为 0，按 0 正常计价（金额 0.0），不计入未计价条数。
    """
    buckets = {}
    for row in model_rows:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        if not isinstance(date, str) or not date:
            continue
        bucket = buckets.setdefault(date, {
            "cost": 0.0, "credits": 0.0, "saved": 0.0,
            "priced_requests": 0, "unpriced_requests": 0,
        })
        requests = int(row.get("requests") or 0)
        priced = pricing.compute(row, table, rate)
        if priced is None:
            bucket["unpriced_requests"] += requests
            continue
        bucket["priced_requests"] += requests
        bucket["cost"] += priced["cost"]
        bucket["credits"] += priced["credits"]
        bucket["saved"] += priced["saved"]
    priced_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        extra = buckets.get(row.get("date")) or {}
        priced_requests = int(extra.get("priced_requests") or 0)
        priced_rows.append({
            **row,
            "cost": extra.get("cost", 0.0) if priced_requests else None,
            "credits": extra.get("credits", 0.0) if priced_requests else None,
            "saved": extra.get("saved", 0.0) if priced_requests else None,
            "unpriced_requests": int(extra.get("unpriced_requests") or 0),
        })
    return priced_rows


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


def _validate_pricing(raw: str) -> str:
    """校验待写入的单价表；合法返回空串，非法返回错误消息。

    复用 pricing 的字段定义与数值校验，避免两处规则漂移。
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        return f"CODEARTS_PRICING 不是合法 JSON（{error}）"
    if not isinstance(data, dict):
        return "CODEARTS_PRICING 顶层必须是对象"
    for model, item in data.items():
        if not isinstance(model, str) or not model:
            return "CODEARTS_PRICING 的模型键必须是非空字符串"
        if not isinstance(item, dict):
            return f"CODEARTS_PRICING 中 {model} 的配置必须是对象"
        for field in pricing._REQUIRED_FIELDS:
            if field not in item:
                return f"CODEARTS_PRICING 中 {model} 缺少必填单价 {field}"
        for field in pricing._PRICE_FIELDS:
            if field in item and not pricing._valid_number(item[field]):
                return f"CODEARTS_PRICING 中 {model} 的 {field} 必须是非负有限数值"
    return ""


def _validate_credits_rate(raw: str) -> str:
    """校验 Credits 换算系数；合法返回空串，非法返回错误消息。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return "CODEARTS_CREDITS_RATE 必须为数值"
    if not math.isfinite(value) or value < 0:
        return "CODEARTS_CREDITS_RATE 必须为非负有限数值"
    return ""


def _validate_require_api_key(raw: str) -> str:
    """校验鉴权开关；只接受 "0" / "1"，避免歧义值被门禁静默当成开启。"""
    if raw not in ("0", "1"):
        return "CODEARTS_REQUIRE_API_KEY 只能为 0 或 1"
    return ""


@blueprint.route("/api/admin/config", methods=["GET"])
def admin_get_config():
    env_map = config_store.as_dict()
    editable = {key: env_map.get(key, _EDITABLE_DEFAULTS.get(key, ""))
                for key in EDITABLE_KEYS}
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
        if key == "CODEARTS_PRICING":
            message = _validate_pricing(value)
        elif key == "CODEARTS_CREDITS_RATE":
            message = _validate_credits_rate(value)
        elif key == "CODEARTS_REQUIRE_API_KEY":
            message = _validate_require_api_key(value)
        else:
            message = ""
        if message:
            return _json_error(message, 400)
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


