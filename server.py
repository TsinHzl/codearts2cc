# -*- coding: utf-8 -*-
"""
CodeArts OpenAI 兼容代理
=========================
把华为云 CodeArts 模型接口（openpangu-2.0-pro）包装成标准 OpenAI
Chat Completions API，供 Claude Code / OpenCode / Cline / Cherry Studio
等任何支持自定义 OpenAI Base URL 的工具直接使用。

启动方式:
    pip install -r requirements.txt
    python server.py

默认监听:  http://127.0.0.1:51818
OpenAI Base URL: http://127.0.0.1:51818/v1
"""
import datetime
import hashlib
import hmac
import json
import os
import random
import re
import threading
import time
import urllib.parse

import requests
from flask import Flask, Response, jsonify, request

import config_store
import usage_log

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
config_store.init(CONFIG_PATH)

AK = config_store.get("CODEARTS_AK").strip()
SK = config_store.get("CODEARTS_SK").strip()
if not AK or not SK:
    raise SystemExit("缺少 CODEARTS_AK / CODEARTS_SK，请在 config.json 中配置后重试")

# CodeArts 目标端点（从抓包确认）
BASE_URL = "https://snap-access.cn-north-4.myhuaweicloud.com"
TARGET = BASE_URL + "/api/v2/chat/completions"
HOST = "snap-access.cn-north-4.myhuaweicloud.com"
AGENT_LIST_URL = BASE_URL + "/v1/agent-center/agents/useragents?offset=0&limit=100"
AGENT_DETAIL_PATH = "/v1/agent-center/agents/detail"

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
MODEL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models-cache.json"
)
# AgentCenter 通道模型（决定是否用 benefit 签名；不在其中的模型一律按 benefit 处理，
# 这样 IDE 以后新增免费模型无需改代码，直接传模型名即可）
AGENT_MODELS = list(FALLBACK_AGENT_MODELS)
SUPPORTED_MODELS = list(FALLBACK_MODELS)
MODEL_DETAILS = {}

# 代理监听地址；改端口只需改这里（或改 config.json 的 CODEARTS_PROXY_PORT），启动日志会同步变化
LISTEN_HOST = config_store.get("CODEARTS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(config_store.get("CODEARTS_PROXY_PORT", "51818"))

app = Flask(__name__)

# admin 管理面口令与会话密钥；口令未设置时管理面整体禁用
ADMIN_PASSWORD = config_store.get("ADMIN_PASSWORD").strip()
ADMIN_SECRET_KEY = config_store.get("ADMIN_SECRET_KEY").strip()


def _signed_request(method: str, url: str, body: bytes = b"", extra_headers=None, timeout=30):
    """发送带华为云 SDK-HMAC-SHA256 签名的请求。"""
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"
    canonical_uri = path if path.endswith("/") else path + "/"
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    params.sort(key=lambda item: (item[0], item[1]))
    quote = lambda value: urllib.parse.quote(str(value), safe="-_.~")
    canonical_query = "&".join(
        f"{quote(key)}={quote(value)}" for key, value in params
    )

    headers = {}
    for key, value in (extra_headers or {}).items():
        headers[key.lower()] = str(value)
    headers["host"] = parsed.netloc
    headers.setdefault("content-type", "application/json")
    sdk_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers["x-sdk-date"] = sdk_date

    signed_names = sorted(headers)
    canonical_headers = "".join(
        f"{name}:{headers[name].strip()}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"{method.upper()}\n{canonical_uri}\n{canonical_query}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    string_to_sign = (
        f"SDK-HMAC-SHA256\n{sdk_date}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )
    signature = hmac.new(
        SK.encode(), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    headers["authorization"] = (
        f"SDK-HMAC-SHA256 Access={AK}, SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    return requests.request(
        method.upper(), url, data=body or None, headers=headers, timeout=timeout
    )


def _hmac_headers(body: bytes, benefit: bool = False, model_id: str = "") -> dict:
    """构造华为云 SDK-HMAC-SHA256 签名请求头（仅适用于 POST /api/v2/chat/completions）。

    benefit=True 时按 IDE 免费模型通道附加 maas_type / model-id / model-name 三个头，
    这三个头必须参与签名，否则上游返回 "The model is not registered"。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    sdk_date = now.strftime("%Y%m%dT%H%M%SZ")

    # 注意：华为云签名规范要求消息头名称一律转小写再参与签名
    headers = {
        "host": HOST,
        "content-type": "application/json",
        "x-sdk-date": sdk_date,
    }
    if benefit and model_id:
        headers["maas_type"] = "benefit"
        headers["model-id"] = model_id
        headers["model-name"] = model_id

    signed_names = sorted(headers.keys())
    canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in signed_names)
    signed_headers = ";".join(signed_names)

    # CanonicalRequest: POST + URI(带尾斜杠) + 空查询串 + 规范头 + 签名头 + body哈希
    canonical_uri = "/api/v2/chat/completions/"
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_request = (
        f"POST\n{canonical_uri}\n\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    # StringToSign: 算法 + 时间戳 + CanonicalRequest哈希
    hashed_canonical = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = f"SDK-HMAC-SHA256\n{sdk_date}\n{hashed_canonical}"

    signature = hmac.new(SK.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"SDK-HMAC-SHA256 Access={AK}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


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
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        print("免费模型列表同步失败，将使用兜底列表:", error)
        return []


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
        SUPPORTED_MODELS = [item["id"] for item in discovered]
        MODEL_DETAILS = {item["id"]: item for item in discovered}
        AGENT_MODELS.clear()
        AGENT_MODELS.extend(
            item["id"] for item in discovered if item.get("channel") == "agent"
        )
        _save_model_cache(discovered)
        print("已自动同步模型:", ", ".join(SUPPORTED_MODELS))
        return True
    except (requests.RequestException, ValueError, TypeError, KeyError, OSError) as error:
        print("自动同步模型失败，将使用缓存或兜底列表:", error)
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
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@app.after_request
def after_request(resp: Response) -> Response:
    # admin 管理面不设置宽松 CORS（Allow-Origin: *），避免弱化 Origin 防线；
    # 其余 /v1/* 端点行为保持不变。
    path = request.path or ""
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
# 总退避压在 ~120s 内（首发 + 7 次重试共 8 次尝试）：客户端（Claude Code
# 默认 60-120s）超时先到会作废整条重试链并重发，拉长序列只会陪跑。
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (2, 4, 8, 16, 32, 32, 32)


def _post_chat_with_retry(body: bytes, headers: dict):
    """带退避重试的上游聊天补全 POST：可重试错误只记警告日志。

    可重试状态码（429/500/502/503/504）与连接异常均在本地退避重试
    （2/4/8/16/32 + 32s×2，首发 + 7 次重试共 8 次尝试，总退避约 126s）；
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
            upstream = requests.post(
                TARGET, data=body, headers=headers, stream=True, timeout=600
            )
        except requests.RequestException as error:
            print(f"警告: 上游连接失败: {error}")
            last_response = None
            last_error = error
            continue
        if upstream.status_code not in _RETRYABLE_STATUS:
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
    headers = _hmac_headers(body, benefit=benefit, model_id=model)

    try:
        upstream = _post_chat_with_retry(body, headers)
    except Exception as e:
        return _cors(jsonify({"error": {"message": f"upstream error: {e}"}})), 502

    if upstream.status_code != 200:
        try:
            detail = upstream.text[:2000]
        except Exception:
            detail = ""
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

        resp = Response(generate(), status=200, mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["X-Accel-Buffering"] = "no"
        return _cors(resp)

    # 非流式：无论上游返回 JSON 还是 SSE，都统一聚合成单个 JSON。
    # CodeArts 在排队、重试或特定模型场景下可能即使请求 stream=false
    # 仍返回 text/event-stream；直接转发会导致客户端把 data: 当作 JSON 解析。
    result = _parse_upstream_json(upstream)
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
# 依赖注入：python server.py 启动时本模块是 __main__，anthropic_proxy 若
# 延迟 import server 会产生第二个模块实例（其全局停留在兜底列表），因此
# 在这里显式传入：AGENT_MODELS 传引用、SUPPORTED_MODELS/MODEL_DETAILS 传
# getter（模型刷新时会被重绑定）、签名/解析函数传函数引用。
# ---------------------------------------------------------------------------
import anthropic_proxy

anthropic_proxy.configure(
    agent_models=AGENT_MODELS,
    supported_getter=lambda: SUPPORTED_MODELS,
    details_getter=lambda: MODEL_DETAILS,
    hmac_headers=_hmac_headers,
    parse_upstream_json=_parse_upstream_json,
    refresh_if_needed=_refresh_models_if_needed,
    target=TARGET,
    record_usage=usage_log.record,
)
app.register_blueprint(anthropic_proxy.blueprint)

# ---------------------------------------------------------------------------
# admin 管理界面（/admin + /api/admin/*，口令鉴权；未设 ADMIN_PASSWORD 时禁用）
# 依赖注入同上：SUPPORTED_MODELS/MODEL_DETAILS 传 getter，AGENT_MODELS 传引用；
# benefit 纯函数传引用（server 与 benefit 同缺 AK/SK 即退出，故其顶层
# SystemExit 副作用在运行期不会触发）。
# ---------------------------------------------------------------------------
import admin_ui
import benefit as _benefit


def _session_salt() -> str:
    """读取或首次生成持久化 session 盐（独立文件 .admin_salt，原子写入）。

    盐跨重启不变，是 session 密钥可从口令确定性派生的前提；随机生成保证
    不同部署实例密钥互不相同。原子写（tmp + os.replace）避免多实例并发
    首启时写坏盐文件。IO 失败时回退为本次运行内随机盐（重启需重新登录），
    保证启动可用性。
    """
    salt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".admin_salt")
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
)
app.register_blueprint(admin_ui.blueprint)
if not ADMIN_PASSWORD:
    print("提示: 未设置 ADMIN_PASSWORD，admin 管理界面已禁用")


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


if __name__ == "__main__":
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
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, threaded=True)
