# -*- coding: utf-8 -*-
"""
CodeArts OpenAI 兼容代理
=========================
把华为云 CodeArts 模型接口（openpangu-2.0-pro）包装成标准 OpenAI
Chat Completions API，供 Claude Code / OpenCode / Cline / Cherry Studio
等任何支持自定义 OpenAI Base URL 的工具直接使用。

启动方式:
    pip install -r requirements.txt
    python -m src.server

默认监听:  http://127.0.0.1:51818
OpenAI Base URL: http://127.0.0.1:51818/v1
"""
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import threading
import time
import urllib.parse

import requests
from flask import Flask, Response, jsonify, request
from markupsafe import escape

from src import paths
from src.auth import api_keys
from src.config import config_store
from src.oauth import oauth_login
from src.usage import pricing
from src.usage import usage_log

CONFIG_PATH = paths.CONFIG_PATH
config_store.init(CONFIG_PATH)

# API Key 存储与 config.json 同目录；文件缺失视为空列表（首次使用由 admin 页面创建）
API_KEYS_PATH = paths.API_KEYS_PATH
api_keys.init(API_KEYS_PATH)


# CodeArts 目标端点（从抓包确认）
BASE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
TARGET = BASE_URL + "/api/v2/chat/completions"
AGENT_LIST_URL = BASE_URL + "/v1/agent-center/agents/useragents?offset=0&limit=100"
AGENT_DETAIL_PATH = "/v1/agent-center/agents/detail"
BUILTIN_MODELS_URL = BASE_URL + "/v1/model/builtin"

# 自动同步失败时的兜底列表（仅老套餐模型，走 AgentCenter 通道）；正常启动会用云端返回的列表覆盖它
FALLBACK_AGENT_MODELS = ["openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"]
# 免费模型兜底列表（IDE 抓包确认）：走 maas_type=benefit 通道，签名时必须额外携带
# maas_type / model-id / model-name 三个头，否则报 "model is not registered"；
# 老套餐模型带上这些头反而会报 "unsupported model"，因此两条通道严格区分。
BENEFIT_MODELS = [
    "glm-5.3-flash",
    "deepseek-v4-pro-0813",
    "deepseek-v4-flash-0731",
]
FALLBACK_MODELS = FALLBACK_AGENT_MODELS + BENEFIT_MODELS
# opengw 免费额度网关（IDE 抓包确认）：永久 AK/SK 即可访问，gateway/config 返回免费模型列表
OPENGW_BASE = "https://opengw.developer.huaweicloud.com"
OPENGW_CONFIG_URL = OPENGW_BASE + "/api/v1/gateway/config"
# 模型缓存与代理程序放在同一目录，便于迁移、备份和排查
MODEL_CACHE_PATH = paths.MODEL_CACHE_PATH
# AgentCenter 通道模型（决定是否用 benefit 签名；不在其中的模型一律按 benefit 处理，
# 这样 IDE 以后新增免费模型无需改代码，直接传模型名即可）
AGENT_MODELS = list(FALLBACK_AGENT_MODELS)
SUPPORTED_MODELS = list(FALLBACK_MODELS)
MODEL_DETAILS = {}

# 代理监听地址；改端口只需改这里（或改 config.json 的 CODEARTS_PROXY_PORT），启动日志会同步变化
LISTEN_HOST = config_store.get("CODEARTS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(config_store.get("CODEARTS_PROXY_PORT", "51818"))

# OAuth 凭据存储（与 config.json 同目录）；缺失视为未登录，损坏降级（见 oauth_login.init）
OAUTH_CRED_PATH = paths.OAUTH_CRED_PATH
oauth_login.init(OAUTH_CRED_PATH)
# 浏览器登录会话仓库（start/poll/import-callback/logout 端点与回调共用，仅内存）
oauth_sessions = oauth_login.OAuthSessionStore()

app = Flask(__name__)

# 访问日志脱敏（design D13）：query string 中的 code/secret/ticket_id/state 值
# 一律替换为 ***，避免 OAuth 凭据落进 werkzeug 访问日志。
_SENSITIVE_QUERY_KEYS = frozenset(("code", "secret", "ticket_id", "state"))
_QUERY_PAIR_RE = re.compile(r"([?&;])([A-Za-z0-9_.%~\-]+)=([^&#\s\"'<>]*)")


def _sanitize_log_line(text: str) -> str:
    """对整行访问日志中的敏感 query 值做 k=*** 替换。"""
    return _QUERY_PAIR_RE.sub(
        lambda m: (
            f"{m.group(1)}{m.group(2)}=***"
            if m.group(2) in _SENSITIVE_QUERY_KEYS else m.group(0)
        ),
        text,
    )


def _needs_sanitize(text: str) -> bool:
    return any(key in text for key in _SENSITIVE_QUERY_KEYS)


class _OAuthQueryFilter(logging.Filter):
    """werkzeug 访问日志过滤器：改写敏感 query 值。

    werkzeug 3.x 的请求行（含 query）位于 record.args[0]、msg 为格式串；
    msg 与 args 双路径都检查，兼容预插值形态。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            new_args = tuple(
                _sanitize_log_line(a) if isinstance(a, str) and _needs_sanitize(a)
                else a
                for a in record.args
            )
            if new_args != record.args:
                record.args = new_args
        if isinstance(record.msg, str) and _needs_sanitize(record.msg):
            record.msg = _sanitize_log_line(record.msg)
        return True


# 注册前移除同类实例：防测试重复 import server 累积 filter（幂等但脏状态）
_werkzeug_logger = logging.getLogger("werkzeug")
_werkzeug_logger.filters[:] = [
    f for f in _werkzeug_logger.filters
    if not isinstance(f, _OAuthQueryFilter)
]
_werkzeug_logger.addFilter(_OAuthQueryFilter())

# admin 管理面口令与会话密钥；口令未设置时管理面整体禁用
ADMIN_PASSWORD = config_store.get("ADMIN_PASSWORD").strip()
ADMIN_SECRET_KEY = config_store.get("ADMIN_SECRET_KEY").strip()


def _signed_request(method: str, url: str, body: bytes = b"", extra_headers=None, timeout=30):
    """发送带华为云 SDK-HMAC-SHA256 签名的请求（模型发现类 GET）。"""
    headers = oauth_login.sign_headers(method, url, body, extra_headers=extra_headers)
    return requests.request(
        method.upper(), url, data=body or None, headers=headers, timeout=timeout
    )


def _hmac_headers(body: bytes, benefit: bool = False, model_id: str = "") -> dict:
    """构造华为云 SDK-HMAC-SHA256 签名请求头（仅适用于 POST /api/v2/chat/completions）。

    benefit=True 时按 IDE 免费模型通道附加 maas_type / model-id / model-name 三个头，
    这三个头必须参与签名，否则上游返回 "The model is not registered"。
    """
    extra = {}
    if benefit and model_id:
        extra = {"maas_type": "benefit", "model-id": model_id, "model-name": model_id}
    return oauth_login.sign_headers("POST", TARGET, body, extra_headers=extra)


def _load_model_cache() -> bool:
    """加载本地模型缓存，返回是否成功。"""
    global SUPPORTED_MODELS, MODEL_DETAILS
    try:
        with open(MODEL_CACHE_PATH, "r", encoding="utf-8") as file:
            cached = json.load(file)
        models = cached.get("models", [])
        if not models:
            return False
        ids = [
            item["id"] for item in models
            if isinstance(item, dict) and item.get("id")
        ]
        if not ids:
            return False  # 缓存无有效条目：保持兜底列表，不清空全局
        SUPPORTED_MODELS = ids
        MODEL_DETAILS = {
            item["id"]: item for item in models
            if isinstance(item, dict) and item.get("id")
        }
        # 缓存里的 AgentCenter 模型参与通道判断；旧缓存无 channel 字段时默认
        # 视为 agent 通道；���在缓存里的新模型一律按 benefit 处理。
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"]
            for item in models
            if isinstance(item, dict) and item.get("channel", "agent") == "agent"
        )
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _save_model_cache(models) -> None:
    os.makedirs(os.path.dirname(MODEL_CACHE_PATH), exist_ok=True)
    payload = {
        "updated_at": int(time.time()),
        "models": models,
    }
    temp_path = MODEL_CACHE_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, MODEL_CACHE_PATH)


def _refresh_benefit_models() -> list:
    """从 opengw 免费额度网关动态获取免费模型列表（IDE 抓包确认的接口）。

    返回模型详情列表；失败时返回空列表，由调用方回退到 BENEFIT_MODELS 兜底。
    """
    try:
        response = _signed_request("GET", OPENGW_CONFIG_URL, timeout=30)
        response.raise_for_status()
        result = response.json().get("result") or {}
        models = result.get("models") or []
        discovered = []
        for model in models:
            model_id = model.get("model_id")
            if not model_id or any(item["id"] == model_id for item in discovered):
                continue
            discovered.append(
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "codearts",
                    "channel": "benefit",
                    "name": model.get("model_name") or model_id,
                    "description": "免费额度模型（签到领取）",
                    "context_window": model.get("context_window"),
                    "max_tokens": model.get("max_tokens"),
                    "supports_images": False,
                }
            )
        return discovered
    except (requests.RequestException, ValueError, TypeError, KeyError,
            oauth_login.CredentialsUnavailable) as error:
        print("免费模型列表同步失败，将使用兜底列表:", type(error).__name__)
        return []


def _fetch_official_credit_pricing() -> dict:
    """拉取官方模型单价表（PromptCenter 通道），返回 {model_id: 原始单价}。

    只保留 time_slot_id 为 null/default 的档位（多档取首个）。失败返回空字典，
    仅打印警告，不阻塞模型同步。
    """
    try:
        response = _signed_request(
            "GET",
            BUILTIN_MODELS_URL,
            extra_headers={
                "agent-type": "PromptCenter",
                "x-language": "zh-cn",
                "accept": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return {}
        official_table = {}
        for model in (data.get("builtinModels") or []):
            if not isinstance(model, dict):
                continue
            model_id = model.get("model_id")
            if not isinstance(model_id, str) or not model_id:
                continue
            tier = pricing.pick_default_credit_tier(model.get("credit"))
            if not isinstance(tier, dict):
                continue
            official_table[model_id] = {
                "input_price": tier.get("input_price"),
                "cache_price": tier.get("cache_price"),
                "output_price": tier.get("output_price"),
                "time_slot_id": tier.get("time_slot_id"),
            }
        return official_table
    except Exception as error:  # best-effort：任何异常都不得阻塞模型同步
        print("官方单价表拉取失败，本次跳过:", type(error).__name__)
        return {}


def _refresh_models() -> bool:
    """从 CodeArts AgentCenter 获取当前账号可用模型（仅老套餐通道）。"""
    global SUPPORTED_MODELS, MODEL_DETAILS
    try:
        # 与 CodeArts CLI 抓包一致：AgentCenter 接口需要这个路由头。
        response = _signed_request(
            "GET",
            AGENT_LIST_URL,
            extra_headers={
                "agent-type": "AgentCenter",
                "x-language": "zh-cn",
                "accept": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        agents = data.get("agents", [])
        if not agents:
            return False

        # 优先主 Agent；若某个详情无模型，再尝试其他主 Agent。
        candidates = sorted(
            agents,
            key=lambda item: (
                not bool(item.get("is_primary_agent")),
                item.get("agent_order") is None,
                item.get("agent_order") or 999999,
            ),
        )
        discovered = []
        for agent in candidates:
            agent_id = agent.get("agent_id")
            if not agent_id:
                continue
            detail_url = (
                f"{BASE_URL}{AGENT_DETAIL_PATH}?agent_id="
                f"{urllib.parse.quote(str(agent_id), safe='')}"
            )
            detail_response = _signed_request(
                "GET",
                detail_url,
                extra_headers={
                    "agent-type": "AgentCenter",
                    "x-language": "zh-cn",
                    "accept": "application/json",
                },
                timeout=30,
            )
            if detail_response.status_code != 200:
                continue
            detail = detail_response.json()
            for model in (detail.get("gpts", {}).get("models", []) or []):
                model_id = model.get("model_id") or model.get("model_alias")
                if not model_id or any(item["id"] == model_id for item in discovered):
                    continue
                params = model.get("model_parameters") or {}
                discovered.append(
                    {
                        "id": model_id,
                        "object": "model",
                        "owned_by": "codearts",
                        "channel": "agent",
                        "name": model.get("model_alias") or model.get("model_name") or model_id,
                        "description": model.get("model_desc") or "",
                        "context_window": params.get("context_window"),
                        "max_tokens": params.get("max_tokens"),
                        "supports_images": params.get("supports_images", False),
                        "enable_queue": params.get("enable_queue", False),
                    }
                )
            if discovered:
                # 主 Agent 已拿到完整模型列表，避免不必要请求。
                break

        if not discovered:
            return False
        # benefit 免费模型不在 AgentCenter 列表里，从 opengw 网关动态同步；
        # 同步失败时回退到 BENEFIT_MODELS 兜底，保证 /v1/models 可见。
        benefit_models = _refresh_benefit_models()
        if not benefit_models:
            benefit_models = [
                {"id": model_id, "channel": "benefit", "object": "model",
                 "owned_by": "codearts", "name": model_id}
                for model_id in BENEFIT_MODELS
            ]
        known = {item["id"] for item in discovered}
        for item in benefit_models:
            if item["id"] not in known:
                discovered.append(item)
        # 官方单价表（PromptCenter 通道）随模型列表一并刷新；失败不影响同步。
        official_pricing = _fetch_official_credit_pricing()
        if official_pricing:
            matched = 0
            for item in discovered:
                prices = official_pricing.get(item["id"])
                if prices:
                    item["credit_pricing"] = prices
                    matched += 1
            print(f"官方单价表已合并: {matched}/{len(discovered)} 个模型命中")
        SUPPORTED_MODELS = [item["id"] for item in discovered]
        MODEL_DETAILS = {item["id"]: item for item in discovered}
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"] for item in discovered if item.get("channel") == "agent"
        )
        _save_model_cache(discovered)
        print("已自动同步模型:", ", ".join(SUPPORTED_MODELS))
        return True
    except (requests.RequestException, ValueError, TypeError, KeyError, OSError,
            oauth_login.CredentialsUnavailable) as error:
        print("自动同步模型失败，将使用缓存或兜底列表:", type(error).__name__)
        return False


# 模型刷新串行化（threaded=True 并发保护）：并发刷新会重复请求上游、
# 交错写坏缓存 tmp，且全局重绑定与 AGENT_MODELS 原地变更间存在不一致窗口
_refresh_lock = threading.Lock()
_last_refresh = 0.0  # time.monotonic()；0 表示从未刷新


def _refresh_models_if_needed() -> None:
    """模型列表短时缓存，避免 Cherry Studio 频繁轮询时重复请求。"""
    global _last_refresh
    # 进程刚启动时先把缓存详情装载到内存；不能只依赖兜底模型名。
    if not MODEL_DETAILS:
        _load_model_cache()
    if time.monotonic() - _last_refresh <= 300:
        return
    with _refresh_lock:
        if time.monotonic() - _last_refresh <= 300:
            return
        try:
            # 失败不回退 _load_model_cache：缓存可能已过期或损坏，
            # 全局保持当前列表；下个 TTL 周期再试云端同步
            _refresh_models()
        finally:
            _last_refresh = time.monotonic()


def _force_refresh_models() -> bool:
    """admin 强制刷新：与 TTL 刷新共用串行锁，防止并发写坏缓存。"""
    global _last_refresh
    with _refresh_lock:
        try:
            return _refresh_models()
        finally:
            _last_refresh = time.monotonic()


def _normalize_finish_reason(value):
    """把 CodeArts 的结束原因转换为 OpenAI 客户端能识别的值。"""
    if value in (None, "other"):
        return "stop"
    return value


def _parse_sse_bytes(raw: bytes):
    """把上游 SSE 聚合为一个 OpenAI chat.completion JSON。"""
    text_parts = []
    reasoning_parts = []
    tool_calls = {}
    result = {
        "id": None,
        "object": "chat.completion",
        "created": None,
        "model": None,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": ""},
            "finish_reason": "stop",
        }],
    }
    usage = None
    last_finish_reason = None

    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].lstrip()
        if payload == b"[DONE]":
            continue
        try:
            chunk = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue

        # CodeArts 可能以 HTTP 200 返回业务错误，需要保留错误信息。
        if chunk.get("error_code") or chunk.get("error_msg"):
            return {
                "error": {
                    "message": chunk.get("error_msg") or "CodeArts upstream error",
                    "type": "upstream_error",
                    "code": chunk.get("error_code"),
                }
            }

        for key in ("id", "created", "model"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices") or []:
            reason = choice.get("finish_reason")
            if reason is not None:
                last_finish_reason = _normalize_finish_reason(reason)
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                current = tool_calls.setdefault(index, {
                    "id": call.get("id"),
                    "type": call.get("type", "function"),
                    "function": {"name": "", "arguments": ""},
                })
                if call.get("id"):
                    current["id"] = call["id"]
                if call.get("type"):
                    current["type"] = call["type"]
                function = call.get("function") or {}
                if function.get("name"):
                    current["function"]["name"] += function["name"]
                if function.get("arguments"):
                    current["function"]["arguments"] += function["arguments"]

    message = result["choices"][0]["message"]
    message["content"] = "".join(text_parts)
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    result["choices"][0]["finish_reason"] = last_finish_reason or ("tool_calls" if tool_calls else "stop")
    if usage:
        result["usage"] = usage
    return result


def _parse_upstream_json(upstream):
    """兼容上游普通 JSON 和错误地返回 SSE 的情况。"""
    raw = upstream.content
    try:
        result = json.loads(raw.decode("utf-8"))
        if isinstance(result, dict) and result.get("choices"):
            for choice in result["choices"]:
                choice["finish_reason"] = _normalize_finish_reason(choice.get("finish_reason"))
        return result
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _parse_sse_bytes(raw)


# 回环 Origin 白名单（含任意端口）：本地浏览器工具（LobeChat 等）跨域可用；
# 公网恶意页面预检失败且响应不可读。CLI 客户端不发 Origin 头，不受影响。
_LOOPBACK_ORIGIN_RE = re.compile(
    r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
)


def _cors(resp: Response) -> Response:
    origin = request.headers.get("Origin", "")
    if _LOOPBACK_ORIGIN_RE.match(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
        # x-api-key 必须出现在预检白名单，否则浏览器在 OPTIONS 阶段就拦下请求
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, x-api-key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ---------------- /v1/* API Key 鉴权门禁 ----------------
# 挂 app 级 before_request：所有 /v1/* 端点（含 anthropic_proxy 蓝图）自动覆盖，
# 后续新增端点不会漏鉴权。豁免项：非 /v1/ 路径（管理面/静态资源）、OPTIONS 预检、
# 管理面禁用（无 ADMIN_PASSWORD）以及配置开关关闭。

_API_KEY_MISSING_EMPTY = (
    "尚未创建任何 API Key，请先在 /admin 的「API Key」页生成一个"
)
_API_KEY_MISSING = (
    "缺少 API Key，请在请求头 x-api-key 或 Authorization: Bearer 中提供；"
    "可在 /admin 的「API Key」页创建"
)
_API_KEY_INVALID = "无效的 API Key"
_API_KEY_DISABLED = "该 API Key 已被禁用，请在 /admin 的「API Key」页启用或重新生成"


def _extract_api_key() -> str:
    """取候选 key：x-api-key 优先，Authorization: Bearer 次之。"""
    candidate = request.headers.get("x-api-key", "")
    if candidate:
        return candidate
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()
    return ""


def _api_key_error(message: str, status: int, code: str) -> Response:
    """按端点协议返回错误体：Anthropic（/v1/messages*）或 OpenAI。"""
    if (request.path or "").startswith("/v1/messages"):
        error_type = "permission_error" if status == 403 else "authentication_error"
        payload = {
            "type": "error",
            "error": {"type": error_type, "message": message},
        }
    else:
        payload = {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": code,
            }
        }
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        content_type="application/json; charset=utf-8",
    )


@app.before_request
def _api_key_gate():
    """校验 /v1/* 请求携带的 API Key；返回 None 表示放行。"""
    path = request.path or ""
    if not path.startswith("/v1/"):
        return None
    if request.method == "OPTIONS":
        return None
    if not ADMIN_PASSWORD:
        return None  # 管理面禁用时无法创建 key，强制鉴权会永久锁死
    if config_store.get("CODEARTS_REQUIRE_API_KEY", "1").strip() == "0":
        return None
    candidate = _extract_api_key()
    if not candidate:
        if not api_keys.list_keys():
            return _api_key_error(_API_KEY_MISSING_EMPTY, 401, "invalid_api_key")
        return _api_key_error(_API_KEY_MISSING, 401, "invalid_api_key")
    status, _ = api_keys.authenticate(candidate)
    if status == api_keys.AUTH_DISABLED:
        return _api_key_error(_API_KEY_DISABLED, 403, "api_key_disabled")
    if status != api_keys.AUTH_VALID:
        return _api_key_error(_API_KEY_INVALID, 401, "invalid_api_key")
    return None


# ---------------------------------------------------------------------------
# OAuth 浏览器登录回调（design D7：双阶段处理，豁免口令门禁与 /v1/* gate）
# ---------------------------------------------------------------------------

def _is_loopback_host(host: str) -> bool:
    """Host 头的主机名是否回环（localhost 或回环 IP 字面量，含 IPv6）。"""
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _oauth_html(title: str, message: str = "") -> str:
    """回调结果页：参数文本一律转义（markupsafe.escape）。"""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>" + str(escape(title)) + "</title></head><body>"
        "<h3>" + str(escape(title)) + "</h3>" +
        ("<p>" + str(escape(message)) + "</p>" if message else "") +
        "</body></html>"
    )


@app.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """portal 双阶段回调：secret 阶段 307 回跳授权页，code 阶段按候选换码。

    浏览器跳转不携带管理会话 cookie/令牌，故注册在 app 上而非管理蓝图；
    回环双重校验（Host 主机名 + remote_addr）不满足一律 403，不处理任何参数。
    """
    # urlsplit 正确剥离 [::1]:51818 形态的端口，避免 split(":") 切坏 IPv6
    host_header = urllib.parse.urlsplit(f"//{request.host or ''}").hostname or ""
    try:
        remote_loopback = ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        remote_loopback = False
    if not _is_loopback_host(host_header) or not remote_loopback:
        return Response(_oauth_html("拒绝访问", "回调来源不是本机回环地址"), status=403)

    code = request.args.get("code", "")
    secret = request.args.get("secret", "")
    redirect = request.args.get("redirect", "")

    # 首次回调：把 portal 下发的 secret 换进会话并开启 ticket 兜底，307 继续授权流程
    if secret and redirect:
        try:
            target = urllib.parse.urlparse(redirect)
        except ValueError:  # 畸形输入（如未闭合 IPv6 括号）
            return Response(_oauth_html("回调被拒绝", "重定向目标非法"), status=400)
        portal = urllib.parse.urlparse(oauth_login.PORTAL_HOST)
        # path 为 portal 前缀 + /oauth/callback（PORTAL_HOST 带 /portal 路径前缀）
        expected_path = portal.path.rstrip("/") + "/oauth/callback"
        if (target.scheme != "https" or target.username is not None
                or (target.hostname or "").lower() != (portal.hostname or "").lower()
                or target.path != expected_path
                or any(ch in redirect for ch in "\r\n\x00")):  # 防 header 注入
            return Response(_oauth_html("回调被拒绝", "重定向目标非法"), status=400)
        ticket_id = urllib.parse.parse_qs(target.query).get("ticket_id", [""])[0]
        if not ticket_id:
            # 空 ticket_id 会让 candidates 退化为全部活跃会话，被 drive-by 覆写 secret
            return Response(_oauth_html("回调被拒绝", "缺少 ticket_id"), status=400)
        for session in oauth_sessions.candidates(ticket_id=ticket_id):
            oauth_sessions.enable_ticket_fallback(session.id, secret)
        # 不回显原始 redirect，用白名单参数重建 Location，彻底关闭注入面
        safe_location = (
            f"https://{portal.hostname}{expected_path}?"
            + urllib.parse.urlencode({"ticket_id": ticket_id})
        )
        return Response(
            _oauth_html("正在继续授权…", "如未自动跳转，请回管理页等待结果"),
            status=307, headers={"Location": safe_location},
        )

    if not code:
        return Response(_oauth_html("登录失败", "缺少授权参数，请回管理页重新发起登录"), status=400)

    # 第二次回调：授权码与 PKCE verifier 一对一校验，用错只是换码失败不消耗授权码，
    # 因此逐候选尝试；首个成功即结束会话并持久化。
    candidates = oauth_sessions.candidates(
        ticket_id=request.args.get("ticket_id", ""), secret=secret,
    )
    if not candidates:
        return Response(_oauth_html("登录已提交", "请回到管理页等待登录结果。"))
    token_response = None
    winner = None
    last_error = ""
    for candidate in candidates:
        try:
            token_response = oauth_login.exchange_code(
                code, candidate.verifier, candidate.port, candidate.dpop_private_key,
            )
            winner = candidate
            break
        except (OSError, ValueError, oauth_login.RefreshTokenExpiredError) as error:
            last_error = type(error).__name__  # 只保留类型名，不含敏感值
    if winner is None:
        return Response(
            _oauth_html("换取凭据失败", f"上游错误（{last_error}），请回管理页重新发起登录"),
            status=502,
        )
    try:
        oauth_login.save_credentials(
            token_response, code_verifier=winner.verifier,
            dpop_private_jwk=winner.dpop_private_key,
        )
    except (OSError, ValueError) as error:
        return Response(
            _oauth_html("保存凭据失败", f"{type(error).__name__}，请检查磁盘权限后重试"),
            status=502,
        )
    oauth_sessions.complete(winner.id)
    return Response(_oauth_html("登录成功", "可关闭此页面并回到管理页。"))


@app.after_request
def after_request(resp: Response) -> Response:
    # admin 管理面不设置宽松 CORS（Allow-Origin: *），避免弱化 Origin 防线；
    # 其余 /v1/* 端点行为保持不变。
    path = request.path or ""
    if path.startswith("/oauth/callback"):
        # 回调 URL 含 code/secret/ticket_id，禁止任何缓存与 Referer 外泄（design D7）
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
    if path == "/admin" or path.startswith("/api/admin"):
        return resp
    return _cors(resp)


def _model_payload(model_id: str) -> dict:
    """生成 OpenAI 模型对象，并附带常见能力元数据。"""
    detail = MODEL_DETAILS.get(model_id, {})
    context_window = detail.get("context_window")
    max_tokens = detail.get("max_tokens")
    supports_images = bool(detail.get("supports_images", False))
    payload = {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "codearts",
        "name": detail.get("name") or model_id,
        "description": detail.get("description") or "",
        # 常见 OpenAI 兼容客户端会读取其中一部分；未知字段会被安全忽略。
        "context_window": context_window,
        "context_length": context_window,
        "max_tokens": max_tokens,
        "max_output_tokens": max_tokens,
        "supports_images": supports_images,
        "vision": supports_images,
        "supports_vision": supports_images,
        "supports_function_calling": True,
        "supports_tool_calling": True,
        "supports_reasoning": True,
        "input_modalities": ["text", "image"] if supports_images else ["text"],
        "output_modalities": ["text"],
        "supported_parameters": [
            "temperature",
            "top_p",
            "max_tokens",
            "stream",
            "tools",
            "tool_choice",
            "response_format",
        ],
    }
    return {key: value for key, value in payload.items() if value is not None}


@app.route("/v1/models", methods=["GET", "OPTIONS"])
def list_models():
    if request.method == "OPTIONS":
        return _cors(Response(""))
    _refresh_models_if_needed()
    return jsonify({
        "object": "list",
        "data": [_model_payload(model_id) for model_id in SUPPORTED_MODELS],
    })


@app.route("/v1/models/<path:model_id>", methods=["GET", "OPTIONS"])
def get_model(model_id):
    """提供标准 OpenAI 单模型详情接口，方便客户端二次读取能力。"""
    if request.method == "OPTIONS":
        return _cors(Response(""))
    _refresh_models_if_needed()
    if model_id not in SUPPORTED_MODELS:
        return _cors(jsonify({
            "error": {
                "message": f"model '{model_id}' not found",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }
        })), 404
    return jsonify(_model_payload(model_id))


# 上游并发超限（429/5xx）时的本地退避重试参数：
# CodeArts 免费通道有并发上限，超限报错是瞬时性的，直接 502 会打断
# Claude Code 会话；本地退避重试把抖动消化在代理侧。
# 仅纳入瞬时性 5xx（500/502/503/504），排除 501/505 等确定性错误。
# 单次退避上限 16s（指数 2/4/8/16 截断，总退避约 30s）：上游恢复通常
# 在秒级抖动内，过长退避只会拉长客户端等待，交给其重发更划算。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (2, 4, 8, 16)
_CONCURRENCY_LIMIT_KEYWORDS = (
    "最多",
    "并发",
    "too many",
    "rate limit",
    "ratelimit",
    "throttl",
    "concurrent",
    "concurrency",
    "quota",
    "busy",
)

# 本地并发门控默认参数（对齐 CodeArts 账号级 3 并发上限）：
# 超出上限的请求在本地 Condition 队列等待，避免第 4 个请求直接打穿上游触发 502。
DEFAULT_MAX_CONCURRENCY = 3
DEFAULT_QUEUE_TIMEOUT = 120.0

_concurrency_cond = threading.Condition(threading.Lock())
_in_flight_requests = 0


class UpstreamQueueTimeoutError(RuntimeError):
    """本地并发排队等待槽位超时。"""


def _max_concurrency() -> int:
    """读取本地最大并发配置（默认 3；<=0 表示不限制，非法值回退默认 3）。"""
    raw = config_store.get("CODEARTS_MAX_CONCURRENCY", str(DEFAULT_MAX_CONCURRENCY))
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENCY


def _queue_timeout() -> float:
    """读取本地并发排队等待超时秒数（默认 120s；非法或 <=0 回退默认 120s）。"""
    raw = config_store.get("CODEARTS_QUEUE_TIMEOUT", str(int(DEFAULT_QUEUE_TIMEOUT)))
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_TIMEOUT
    return value if value > 0 else DEFAULT_QUEUE_TIMEOUT


class _UpstreamLease:
    """幂等上游并发槽位与响应连接租约：重复 release() 仅归还一次槽位并关闭连接。"""

    def __init__(self, counted: bool = True) -> None:
        self._counted = counted
        self._released = False
        self._upstream = None
        self._lock = threading.Lock()

    def attach(self, upstream):
        """绑定或替换当前持有的上游响应对象；若租约已释放或存在旧响应则立即关闭。"""
        old = None
        with self._lock:
            if self._released:
                old = upstream
            elif self._upstream is not None and self._upstream is not upstream:
                old = self._upstream
                self._upstream = upstream
            else:
                self._upstream = upstream
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        return upstream

    def release(self) -> None:
        """幂等关闭绑定的 upstream 响应并归还并发槽位。"""
        global _in_flight_requests
        with self._lock:
            if self._released:
                return
            self._released = True
            upstream = self._upstream
            self._upstream = None
            counted = self._counted
        if upstream is not None:
            try:
                upstream.close()
            except Exception:
                pass
        if counted:
            with _concurrency_cond:
                _in_flight_requests = max(0, _in_flight_requests - 1)
                _concurrency_cond.notify()

    def __enter__(self) -> "_UpstreamLease":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def _acquire_upstream_lease(timeout: float | None = None) -> _UpstreamLease:
    """获取一个上游并发槽位租约；超出上限时在本地排队等待，超时抛 UpstreamQueueTimeoutError。"""
    global _in_flight_requests
    limit = _max_concurrency()
    if limit <= 0:
        return _UpstreamLease(counted=False)
    wait_timeout = _queue_timeout() if timeout is None else float(timeout)
    deadline = time.monotonic() + wait_timeout
    with _concurrency_cond:
        while _in_flight_requests >= limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpstreamQueueTimeoutError(
                    f"本地并发排队超时（上限 {limit} 并发，等待超过 {wait_timeout:g}s）"
                )
            _concurrency_cond.wait(remaining)
            limit = _max_concurrency()
            if limit <= 0:
                return _UpstreamLease(counted=False)
        _in_flight_requests += 1
        return _UpstreamLease(counted=True)


def _has_concurrency_keyword(text: str) -> bool:
    """判断错误文本是否包含并发或限流特征词。"""
    lowered = (text or "").lower()
    return any(keyword in lowered for keyword in _CONCURRENCY_LIMIT_KEYWORDS)


def _is_retryable_upstream(upstream) -> bool:
    """判断上游响应是否属于可退避重试的瞬时错误或并发超限。"""
    status = getattr(upstream, "status_code", None)
    if status in _RETRYABLE_STATUS:
        return True
    if status in (400, 403):
        try:
            return _has_concurrency_keyword(upstream.text or "")
        except Exception:
            return False
    if status == 200:
        headers = getattr(upstream, "headers", None) or {}
        content_type = (headers.get("Content-Type") or "").lower()
        if "text/event-stream" in content_type:
            return False
        try:
            raw_text = getattr(upstream, "text", None)
            if raw_text is None and getattr(upstream, "content", None) is not None:
                raw_text = upstream.content.decode("utf-8", errors="ignore")
            data = json.loads(raw_text or "")
        except Exception:
            return False
        if isinstance(data, dict) and (data.get("error") or data.get("error_code") or data.get("error_msg")):
            return _has_concurrency_keyword(json.dumps(data, ensure_ascii=False))
    return False


def _post_chat_with_retry(body: bytes, headers_or_fn):
    """带退避重试的上游聊天补全 POST：可重试错误只记警告日志。

    可重试状态码（429/500/502/503/504）与连接异常均在本地退避重试
    （2/4/8/16s，首发 + 4 次重试共 5 次尝试，总退避约 30s）；
    重试耗尽后：最后一次拿到响应则返回它（交由调用方既有 502 路径承接），
    全程连接失败则抛最后一次 RequestException。
    注意：chat completion 非幂等，极端情况下重试可能重复生成/计费，属已知取舍。
    """
    last_response = None
    last_error = None
    for attempt, delay in enumerate((0.0,) + _RETRY_DELAYS):
        if delay:
            status_hint = getattr(last_response, "status_code", None) or "连接失败"
            print(f"警告: 上游错误({status_hint})，{delay}s 后重试（第 {attempt}/{len(_RETRY_DELAYS)} 次）...")
            # 加随机抖动避免并发请求同步退避形成重试风暴
            time.sleep(delay + random.uniform(0, delay * 0.3))
        try:
            req_headers = headers_or_fn() if callable(headers_or_fn) else headers_or_fn
            upstream = requests.post(
                TARGET, data=body, headers=req_headers, stream=True, timeout=600
            )
        except requests.RequestException as error:
            print(f"警告: 上游连接失败: {error}")
            last_response = None
            last_error = error
            continue
        if not _is_retryable_upstream(upstream):
            return upstream
        last_response = upstream
        try:
            upstream.close()  # 可重试响应不再读取，释放连接
        except Exception:
            pass
    if last_response is not None:
        return last_response
    raise last_error


@app.route("/v1/chat/completions", methods=["POST", "OPTIONS"])
def chat_completions():
    if request.method == "OPTIONS":
        return _cors(Response(""))

    body = request.get_data()
    if not body:
        return _cors(jsonify({"error": {"message": "empty body"}})), 400
    try:
        req_json = json.loads(body)
    except Exception:
        return _cors(jsonify({"error": {"message": "invalid JSON body"}})), 400

    # 模型名归一化；请求聊天时也按短缓存周期同步一次模型列表
    _refresh_models_if_needed()
    model = req_json.get("model", SUPPORTED_MODELS[0])
    if model not in SUPPORTED_MODELS:
        return (
            _cors(
                jsonify(
                    {
                        "error": {
                            "message": f"model '{model}' 不在当前账号可用模型列表中",
                            "type": "invalid_request_error",
                            "param": "model",
                            "code": "model_not_found",
                        }
                    }
                )
            ),
            404,
        )
    req_json["model"] = model

    want_stream = bool(req_json.get("stream", False))
    request_started = time.monotonic()
    # 流式 generator 在请求上下文销毁后才被迭代，client_ip 必须在此处捕获
    client_ip = request.remote_addr or ""

    # 通道判断：AgentCenter 老套餐模型用基础签名；其余（免费 benefit 模型）
    # 必须带 maas_type/model-id/model-name 三个签名头，否则上游报未注册。
    benefit = model not in AGENT_MODELS

    # 重新序列化 body（确保与签名时一致）
    body = json.dumps(req_json, ensure_ascii=False).encode("utf-8")
    try:
        lease = _acquire_upstream_lease()
    except UpstreamQueueTimeoutError as e:
        return _cors(jsonify({"error": {"message": f"upstream error: {e}"}})), 502

    # _hmac_headers 取 STS 快照，未登录时抛 CredentialsUnavailable：
    # 必须纳入捕获（design D10），否则未登录为 500 而非 503 信封
    try:
        upstream = lease.attach(
            _post_chat_with_retry(
                body,
                lambda: _hmac_headers(body, benefit=benefit, model_id=model),
            )
        )
    except oauth_login.CredentialsUnavailable:
        lease.release()
        return _cors(jsonify({"error": {
            "message": "尚未登录 CodeArts，请先在管理页完成登录",
            "type": "service_unavailable",
            "code": "oauth_not_logged_in",
        }})), 503
    except Exception as e:
        lease.release()
        return _cors(jsonify({"error": {"message": f"upstream error: {e}"}})), 502

    if upstream.status_code != 200:
        try:
            detail = upstream.text[:2000]
        except Exception:
            detail = ""
        finally:
            lease.release()
        return (
            _cors(
                jsonify(
                    {
                        "error": {
                            "message": f"upstream {upstream.status_code}: {detail}"
                        }
                    }
                )
            ),
            502,
        )

    if want_stream:
        def generate():
            """严格转发为 OpenAI SSE；仅对 benefit 流补齐缺失的结束原因。"""
            content_type = (upstream.headers.get("Content-Type") or "").lower()
            is_benefit = model not in AGENT_MODELS
            saw_done = False
            saw_finish_reason = False
            first_token_at = None
            stream_usage = None

            # 上游偶尔会在请求 stream=true 时返回普通 JSON，包装成单个 SSE 帧。
            if "text/event-stream" not in content_type:
                result = _parse_upstream_json(upstream)
                if isinstance(result, dict) and result.get("error"):
                    # HTTP 200 + JSON 错误体：下发错误帧即止，不补 [DONE]，
                    # 与非流式路径的 502 口径对齐
                    yield b"data: " + json.dumps(
                        result, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8") + b"\n\n"
                    return
                for choice in result.get("choices") or []:
                    if choice.get("finish_reason") in (None, "other"):
                        choice["finish_reason"] = "stop"
                payload = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                # 使用日志：上游误回 JSON 的成功补全也计数（包装为单帧流）
                _record_usage(
                    model=model,
                    stream=True,
                    client_ip=client_ip,
                    effort=req_json.get("reasoning_effort"),
                    usage=result.get("usage"),
                    first_token_ms=None,
                    started_at=request_started,
                )
                yield b"data: " + payload + b"\n\n"
                yield b"data: [DONE]\n\n"
                return

            try:
                for raw_line in upstream.iter_lines(decode_unicode=False):
                    if not raw_line:
                        continue
                    if not raw_line.startswith(b"data:"):
                        if raw_line.startswith(b":"):
                            yield raw_line + b"\n\n"
                        continue

                    payload = raw_line[5:].strip()
                    if payload == b"[DONE]":
                        # benefit 流需要先确认是否缺少 finish_reason；agent 流则保持
                        # 原来的即时转发行为，避免改变 agent 客户端状态机。
                        if not is_benefit:
                            if not saw_done:
                                yield b"data: [DONE]\n\n"
                                saw_done = True
                        else:
                            saw_done = True
                        continue
                    try:
                        chunk = json.loads(payload.decode("utf-8"))
                        if chunk.get("usage"):
                            stream_usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            reason = choice.get("finish_reason")
                            if reason is not None:
                                saw_finish_reason = True
                                if reason == "other":
                                    choice["finish_reason"] = "stop"
                            # DeepSeek benefit 流会发送 content:null、reasoning_content 有值。
                            # 部分 Agent Runtime 只接受字符串 content，会将 null chunk
                            # 误判为无响应；OpenAI 兼容格式中空字符串表达同样语义。
                            delta = choice.get("delta")
                            if isinstance(delta, dict):
                                if delta.get("content") is None:
                                    delta["content"] = ""
                                if first_token_at is None and delta.get("content"):
                                    first_token_at = time.monotonic()
                        payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        yield b"data: " + payload + b"\n\n"
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
            except (requests.RequestException, BrokenPipeError) as error:
                print("警告: 上游流中断:", error)
                return  # 客户端连接已建立，记录后终止，按断流处理

            # 只对免费 benefit 模型补齐完全缺失的 finish_reason；不重发任何上游 chunk。
            if is_benefit and not saw_finish_reason:
                yield b"data: " + json.dumps(
                    {
                        "id": "proxy-finish",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }, ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8") + b"\n\n"
            if is_benefit and not saw_done:
                yield b"data: [DONE]\n\n"
            elif is_benefit:
                # 上游 [DONE] 被延迟到这里，确保补块在 [DONE] 之前。
                yield b"data: [DONE]\n\n"
            # agent 分支保持原有行为：如果上游异常断流、没有发送 [DONE]，
            # 代理仍补一个 [DONE]，与修改前的实现一致。
            if not is_benefit and not saw_done:
                yield b"data: [DONE]\n\n"

            # 使用日志：正常收尾（含补块后）记录一次；断流/异常路径不记录
            _record_usage(
                model=model,
                stream=True,
                client_ip=client_ip,
                effort=req_json.get("reasoning_effort"),
                usage=stream_usage,
                first_token_ms=(
                    int((first_token_at - request_started) * 1000)
                    if first_token_at else None
                ),
                started_at=request_started,
            )

        def generate_with_lease():
            try:
                yield from generate()
            finally:
                lease.release()

        resp = Response(generate_with_lease(), status=200, mimetype="text/event-stream")
        resp.call_on_close(lease.release)
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["X-Accel-Buffering"] = "no"
        return _cors(resp)

    # 非流式：无论上游返回 JSON 还是 SSE，都统一聚合成单个 JSON。
    # CodeArts 在排队、重试或特定模型场景下可能即使请求 stream=false
    # 仍返回 text/event-stream；直接转发会导致客户端把 data: 当作 JSON 解析。
    try:
        result = _parse_upstream_json(upstream)
    finally:
        lease.release()
    if isinstance(result, dict) and result.get("error"):
        resp = Response(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            status=502,
            content_type="application/json; charset=utf-8",
        )
        return _cors(resp)
    output = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # 使用日志：非流式成功返回前记录（含 JSON 错误体转 502 之外的成功路径）
    _record_usage(
        model=model,
        stream=False,
        client_ip=client_ip,
        effort=req_json.get("reasoning_effort"),
        usage=result.get("usage"),
        first_token_ms=None,
        started_at=request_started,
    )
    resp = Response(output, status=200, content_type="application/json; charset=utf-8")
    return _cors(resp)


def _record_usage(model: str, stream: bool, client_ip: str, effort, usage,
                  first_token_ms, started_at: float) -> None:
    """组装并写入一条使用日志（双端点共用字段口径；失败不影响主链路）。

    client_ip 须由调用方在请求上下文存活时捕获后传入——流式 generator
    在请求上下文销毁后才执行，内部访问 request 会抛 RuntimeError。
    """
    try:
        usage = usage if isinstance(usage, dict) else {}
        usage_log.record({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "endpoint": "openai",
            "ip": client_ip,
            "model": model,
            "stream": stream,
            "effort": effort if isinstance(effort, str) else None,
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
            "cache_creation_tokens": 0,
            "first_token_ms": first_token_ms,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
        })
    except Exception as error:
        print(f"警告: 使用日志记录失败: {error}")


# ---------------------------------------------------------------------------
# Anthropic Messages 兼容端点（/v1/messages，供 Claude Code 直接接入）
# 依赖注入：python -m src.server 启动时本模块是 __main__，anthropic_proxy 若
# 延迟 import server 会产生第二个模块实例（其全局停留在兜底列表），因此
# 在这里显式传入：AGENT_MODELS 传引用、SUPPORTED_MODELS/MODEL_DETAILS 传
# getter（模型刷新时会被重绑定）、签名/解析函数传函数引用。
# ---------------------------------------------------------------------------
from src.proxy import anthropic_proxy

anthropic_proxy.configure(
    agent_models=AGENT_MODELS,
    supported_getter=lambda: SUPPORTED_MODELS,
    details_getter=lambda: MODEL_DETAILS,
    hmac_headers=_hmac_headers,
    parse_upstream_json=_parse_upstream_json,
    refresh_if_needed=_refresh_models_if_needed,
    target=TARGET,
    record_usage=usage_log.record,
    acquire_lease=_acquire_upstream_lease,
)
app.register_blueprint(anthropic_proxy.blueprint)

# ---------------------------------------------------------------------------
# admin 管理界面（/admin + /api/admin/*，口令鉴权；未设 ADMIN_PASSWORD 时禁用）
# 依赖注入同上：SUPPORTED_MODELS/MODEL_DETAILS 传 getter，AGENT_MODELS 传引用；
# benefit 纯函数传引用（benefit 与 server 共用 OAuth 凭据，未登录时抛
# CredentialsUnavailable，由 admin_ui 转为 503）。
# ---------------------------------------------------------------------------
from src.admin import admin_oauth
from src.admin import admin_ui
from src.tools import benefit as _benefit


def _session_salt() -> str:
    """读取或首次生成持久化 session 盐（独立文件 .admin_salt，原子写入）。

    盐跨重启不变，是 session 密钥可从口令确定性派生的前提；随机生成保证
    不同部署实例密钥互不相同。原子写（tmp + os.replace）避免多实例并发
    首启时写坏盐文件。IO 失败时回退为本次运行内随机盐（重启需重新登录），
    保证启动可用性。
    """
    salt_path = paths.ADMIN_SALT_PATH
    try:
        if os.path.exists(salt_path):
            with open(salt_path, "r", encoding="utf-8") as salt_file:
                salt = salt_file.read().strip()
            if salt:
                return salt
        salt = os.urandom(24).hex()
        tmp_path = salt_path + ".tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as salt_file:
            salt_file.write(salt)
        os.replace(tmp_path, salt_path)  # 原子替换，同 models-cache.json 模式
        print("提示: 已生成 session 盐文件 .admin_salt（用于 session 密钥派生）")
        return salt
    except (OSError, UnicodeDecodeError) as error:
        print(f"警告: session 盐文件读写失败（{error}），本次运行改用临时随机盐，重启后需重新登录")
        return os.urandom(24).hex()


# session 密钥：优先用显式配置的 ADMIN_SECRET_KEY；否则从持久化随机盐
# （.admin_salt，首次启动生成）+ 管理口令派生，保证重启后
# cookie 仍有效（登录一次即自动登录），且密钥不可反推口令、cookie 不能被
# 用作离线爆破口令的 oracle。管理面禁用（口令为空）时无需保持登录态，
# 直接用一次性随机密钥，也不生成盐文件
_ADMIN_SECRET_SOURCE = ADMIN_SECRET_KEY or (
    _session_salt() + ":" + ADMIN_PASSWORD if ADMIN_PASSWORD else os.urandom(32).hex()
)
app.secret_key = hashlib.sha256(_ADMIN_SECRET_SOURCE.encode()).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

admin_ui.configure(
    password=ADMIN_PASSWORD,
    model_list=lambda: SUPPORTED_MODELS,
    model_details=lambda: MODEL_DETAILS,
    agent_models=AGENT_MODELS,
    refresh_models=_force_refresh_models,
    benefit_balance=_benefit.balance,
    benefit_status=_benefit.claim_status,
    benefit_claim=_benefit.claim,
    config_path=CONFIG_PATH,
    upstream=TARGET,
    host=LISTEN_HOST,
    port=LISTEN_PORT,
    cache_path=MODEL_CACHE_PATH,
    usage_log_query=usage_log.query,
    oauth_status=oauth_login.credentials_status,
)
# OAuth 管理端点依赖注入：会话仓库引用 + 监听端口（授权链接 port 参数）
admin_oauth.configure(oauth_sessions, LISTEN_PORT)
app.register_blueprint(admin_ui.blueprint)


def _warn_missing_admin_password() -> None:
    """未设管理口令时打印可操作警告（不退出；管理面禁用、OAuth 无法登录）。"""
    print("警告: 未设置 ADMIN_PASSWORD —— 无法在 /admin 创建 API Key，也无法完成 OAuth 登录")
    print("警告: 请在 config.json 设置 ADMIN_PASSWORD 后重启，再打开 /admin 完成登录")


if not ADMIN_PASSWORD:
    _warn_missing_admin_password()


def _open_admin_page() -> None:
    """server 就绪后自动打开 admin 管理页面（best-effort，失败仅提示）。

    浏览器主机规范化：0.0.0.0 对部分浏览器不可访问，回环展示为 127.0.0.1；
    IPv6 字面量需方括号。
    """
    import webbrowser
    browse_host = "127.0.0.1" if LISTEN_HOST in ("0.0.0.0", "::") else LISTEN_HOST
    if ":" in browse_host:  # IPv6 字面量需方括号
        browse_host = f"[{browse_host}]"
    try:
        opened = webbrowser.open(f"http://{browse_host}:{LISTEN_PORT}/admin")
    except Exception as error:  # 无桌面环境等场景：提示手动访问
        print(f"提示: 自动打开浏览器失败（请手动访问 /admin）: {error}")
        return
    if not opened:
        print("提示: 未能自动打开浏览器，请手动访问 /admin")


# ---------------------------------------------------------------------------
# 凭据续期与启动（design D9）
# ---------------------------------------------------------------------------

RENEWAL_INTERVAL_SECONDS = 10 * 60   # 续期线程轮询间隔（秒）；提前量 30 分钟，最坏仍留 20 分钟余量
STARTUP_REFRESH_TIMEOUT = 10.0       # 启动刷新等待上限（秒）


def _is_serving_process() -> bool:
    """是否应执行后台任务：reloader 子进程，或未启用 reloader 的唯一进程。

    Flask reloader 先起父进程，再以 WERKZEUG_RUN_MAIN=true 起真正服务的
    子进程；仅在后者执行，避免启动刷新与续期线程重复运行。
    """
    return os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug


def _log_refresh_result(prefix: str, result: str,
                        hint_when_unavailable: bool = False) -> None:
    """按 refresh_if_needed 返回值打印可操作日志（不输出任何凭据值）。"""
    if result == "refreshed":
        print(f"{prefix}: OAuth 凭据已续期")
    elif result == "relogin":
        print(f"{prefix}: refresh_token 已失效，请打开 /admin「登录」页签重新登录")
    elif result == "unavailable" and hint_when_unavailable:
        print("提示: 尚未登录 CodeArts，请打开 /admin「登录」页签完成登录")


def _startup_refresh(timeout: float = STARTUP_REFRESH_TIMEOUT) -> None:
    """启动时刷新一次凭据：最多等待 timeout 秒，超时/失败均不阻塞启动。

    刷新请求自身超时 15 秒，这里只限制启动等待时长；超时后后台线程
    仍会完成刷新与落盘。异常只打印类型名，不输出凭据相关内容。
    """
    def _run() -> None:
        try:
            result = oauth_login.refresh_if_needed()
        except Exception as error:  # 兜底：启动刷新失败不得阻塞服务
            print(f"提示: 启动时 OAuth 凭据刷新失败（{type(error).__name__}），已继续启动")
            return
        _log_refresh_result("启动刷新", result, hint_when_unavailable=True)

    worker = threading.Thread(target=_run, name="oauth-startup-refresh", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        print(f"提示: OAuth 凭据刷新超过 {timeout:g} 秒，已继续启动（刷新将在后台完成）")


def _renewal_loop(interval: float = RENEWAL_INTERVAL_SECONDS,
                  stop_event: threading.Event | None = None) -> None:
    """续期线程主体：每 interval 秒按需刷新一次。

    refresh_if_needed 自身负责 30 分钟阈值、5 分钟退避与终态失效；
    此处只按返回值输出日志，单次异常不得终止线程。
    """
    stop_event = stop_event or threading.Event()
    while not stop_event.wait(interval):
        try:
            result = oauth_login.refresh_if_needed()
        except Exception as error:  # 兜底：单次异常不得终止续期线程
            print(f"警告: OAuth 凭据自动续期异常（{type(error).__name__}），下个周期重试")
            continue
        _log_refresh_result("自动续期", result)


def _start_renewal_thread(
    interval: float = RENEWAL_INTERVAL_SECONDS,
) -> threading.Thread | None:
    """启动 daemon 续期线程；reloader 父进程返回 None（design D9）。"""
    if not _is_serving_process():
        return None
    worker = threading.Thread(
        target=_renewal_loop,
        args=(interval,),
        name="oauth-renewal",
        daemon=True,
    )
    worker.start()
    return worker


def _main() -> None:
    """服务启动入口（`__main__` 调用；拆出函数便于测试启动顺序）。"""
    serving = _is_serving_process()
    # 先刷新凭据再同步模型，避免用即将过期的 STS 凭据发起签名请求
    if serving:
        _startup_refresh()
    # 启动时先同步一次真实模型列表（失败则回退到缓存/兜底列表），再打印。
    if not _refresh_models():
        _load_model_cache()
    usage_log.init()  # 回放 usage-log.jsonl 重建使用统计（admin「使用日志」页数据源）
    print(f"CodeArts OpenAI 兼容代理已启动: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"OpenAI Base URL: http://{LISTEN_HOST}:{LISTEN_PORT}/v1")
    print("可用模型:", SUPPORTED_MODELS)
    print("按 Ctrl+C 停止")
    if ADMIN_PASSWORD and config_store.get("CODEARTS_NO_OPEN") != "1":
        open_timer = threading.Timer(1.0, _open_admin_page)
        open_timer.daemon = True  # Timer 不接受 daemon kwarg，只能设实例属性
        open_timer.start()
    if serving:
        _start_renewal_thread()
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)


if __name__ == "__main__":
    _main()
