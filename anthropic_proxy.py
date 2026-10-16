# -*- coding: utf-8 -*-
"""
Anthropic Messages 兼容端点
===========================
把 Anthropic Messages 协议（POST /v1/messages）转换为 OpenAI Chat
Completions，复用 server.py 的 CodeArts 签名与上游链路，使 Claude Code
可以通过 ANTHROPIC_BASE_URL 直接把本代理作为模型后端。

结构（镜像 kiro2cc-proxy 的方向相反实现）：
  1. 请求转换器  anthropic_messages_to_openai
  2. 模型映射    resolve_model（三步法）
  3. 非流式转换  openai_completion_to_anthropic
  4. 流式状态机  AnthropicStreamConverter
  5. 蓝图路由    blueprint + 依赖注入 + 上游怪癖修补层

层间契约：修补层输入上游原始字节流，输出「归一化 OpenAI chunk 序列」
（finish_reason 必有、delta.content 非空串）；转换器假设输入合规。
"""
import json
import os
import time
import urllib.parse

import requests
from flask import Blueprint, Response, request

import cache_sim

# ---------------------------------------------------------------------------
# 依赖注入接口（server.py 注册蓝图时填充；禁止延迟 import server）
# ---------------------------------------------------------------------------

# AGENT_MODELS：原地 .clear()/.extend() 变更，必须传列表引用
AGENT_MODELS = []
# SUPPORTED_MODELS / MODEL_DETAILS：模型刷新时被重绑定，必须传 getter
get_supported_models = lambda: []  # noqa: E731
get_model_details = lambda: {}  # noqa: E731
# 函数引用：不重绑定，直接传函数对象
signed_headers_fn = None  # _hmac_headers(body, benefit, model_id)
parse_upstream_json_fn = None  # _parse_upstream_json(upstream)
refresh_models_if_needed_fn = None  # _refresh_models_if_needed()
TARGET_URL = ""  # 上游聊天补全端点


def configure(agent_models, supported_getter, details_getter,
              hmac_headers, parse_upstream_json, refresh_if_needed, target):
    """由 server.py 在启动时调用，注入依赖（见模块顶部说明）。"""
    global AGENT_MODELS, get_supported_models, get_model_details
    global signed_headers_fn, parse_upstream_json_fn
    global refresh_models_if_needed_fn, TARGET_URL
    AGENT_MODELS = agent_models
    get_supported_models = supported_getter
    get_model_details = details_getter
    signed_headers_fn = hmac_headers
    parse_upstream_json_fn = parse_upstream_json
    refresh_models_if_needed_fn = refresh_if_needed
    TARGET_URL = target


def _anthropic_error(message: str, error_type: str = "invalid_request_error", status: int = 400):
    """构造 Anthropic 错误响应。"""
    payload = json.dumps(
        {"type": "error", "error": {"type": error_type, "message": message}},
        ensure_ascii=False,
    )
    return Response(payload, status=status, content_type="application/json; charset=utf-8")


# ---------------------------------------------------------------------------
# T2 模型映射（三步法）
# ---------------------------------------------------------------------------

# 默认别名表：sonnet 档映射主工作模型，haiku 档映射免费 benefit 模型
DEFAULT_MODEL_MAP = {
    "claude-sonnet-4-5": "openpangu-2.0-pro",
    "claude-haiku-4-5": "glm-5.3-flash",
}
# Claude Code 的 1M 上下文后缀
_CONTEXT_SUFFIX = "[4m]"


def _parse_model_map_env(raw: str) -> dict:
    """解析 CODEARTS_ANTHROPIC_MODEL_MAP（逗号分隔的 alias:model）。"""
    mapping = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        alias, _, target = part.partition(":")
        alias, target = alias.strip(), target.strip()
        if alias and target:
            mapping[alias] = target
    return mapping


def resolve_model(client_model: str) -> str:
    """模型名映射三步法。

    1) 剥离 `[4m]` 1M 上下文后缀；
    2) 查别名表（env 覆盖默认表；空 env 回退默认表）；
    3) `claude-*` 未命中兜底 SUPPORTED_MODELS[0] 并打印警告；
       非 `claude-*` 名称原样返回，交由路由校验。
    """
    if not isinstance(client_model, str):
        client_model = ""
    name = client_model
    if name.endswith(_CONTEXT_SUFFIX):
        name = name[: -len(_CONTEXT_SUFFIX)]

    env_map = os.getenv("CODEARTS_ANTHROPIC_MODEL_MAP", "").strip()
    mapping = _parse_model_map_env(env_map) if env_map else dict(DEFAULT_MODEL_MAP)

    if name in mapping:
        return mapping[name]
    if name.startswith("claude-"):
        supported = get_supported_models()
        fallback = supported[0] if supported else name
        print(f"警告: 模型 '{client_model}' 未在映射表中命中，兜底使用 '{fallback}'")
        return fallback
    return name


# ---------------------------------------------------------------------------
# T1 请求转换器
# ---------------------------------------------------------------------------

def _blocks_to_text(blocks) -> str:
    """把 system/content 的 blocks 形态合并为纯文本。"""
    parts = []
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(part for part in parts if part)


def _tool_result_block_to_messages(block: dict) -> list:
    """单个 tool_result 块 → 独立 role:"tool" OpenAI 消息（不合并）。"""
    content = block.get("content")
    if isinstance(content, list):
        text = _blocks_to_text(content)
    elif isinstance(content, str):
        text = content
    else:
        text = ""
    if block.get("is_error"):
        text = f"[tool error] {text}" if text else "[tool error]"
    return [{
        "role": "tool",
        "tool_call_id": block.get("tool_use_id", ""),
        "content": text,
    }]


def _convert_user_content(blocks) -> list:
    """user turn 的 content blocks → OpenAI messages 列表。

    混合块（tool_result 与 text）按「tool 先、text 后」重排：全部 tool_result
    转为独立 role:"tool" 消息排在前面，text 合并为一条 user 消息垫底，
    满足 OpenAI 的 tool 消息紧随 assistant tool_calls 的约束。
    """
    tool_messages = []
    text_parts = []
    image_messages = []
    for block in blocks or []:
        block_type = block.get("type") if isinstance(block, dict) else None
        if block_type == "tool_result":
            tool_messages.extend(_tool_result_block_to_messages(block))
        elif block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "image":
            image_messages.append(block)
        # 未知块类型静默忽略

    messages = list(tool_messages)
    if text_parts:
        messages.append({"role": "user", "content": "\n".join(text_parts)})
    # image 块仅对 supports_images=True 的模型合法（校验在路由层做），转 OpenAI 格式
    for block in image_messages:
        source = block.get("source") or {}
        messages.append({
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": f"data:{source.get('media_type')};base64,{source.get('data')}"},
            }],
        })
    return messages


def anthropic_messages_to_openai(body: dict, resolve_model_fn) -> dict:
    """Anthropic Messages 请求体 → OpenAI Chat Completions 请求体（纯函数）。"""
    model = resolve_model_fn(body.get("model", ""))

    messages = []
    # system：string 或 blocks 两种形态
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append({"role": "system", "content": system})
    elif isinstance(system, list) and system:
        text = _blocks_to_text(system)
        if text:
            messages.append({"role": "system", "content": text})

    for message in body.get("messages") or []:
        role = message.get("role")
        content = message.get("content")
        if role == "user":
            if isinstance(content, str):
                messages.append({"role": "user", "content": content})
            else:
                messages.extend(_convert_user_content(content))
        elif role == "assistant":
            text_parts = []
            tool_calls = []
            if isinstance(content, str):
                text_parts.append(content)
            else:
                for block in content or []:
                    block_type = block.get("type") if isinstance(block, dict) else None
                    if block_type == "text":
                        text_parts.append(block.get("text", ""))
                    elif block_type == "thinking":
                        # 上游无法回传 thinking；跳过（历史轮次的思考过程不重放）
                        continue
                    elif block_type == "tool_use":
                        tool_calls.append({
                            "id": block.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                            },
                        })
            assistant_message = {"role": "assistant", "content": "\n".join(p for p in text_parts if p) or ""}
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            # OpenAI 约束：tool_calls 消息 content 允许为空串
            messages.append(assistant_message)

    openai_body = {
        "model": model,
        "messages": messages,
        "max_tokens": body.get("max_tokens") or 4096,
        "stream": bool(body.get("stream", False)),
    }
    if body.get("temperature") is not None:
        openai_body["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        openai_body["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        openai_body["stop"] = body["stop_sequences"]
    # tools → OpenAI tools 格式（待验证假设；若上游只认 functions 再调整）
    if body.get("tools"):
        openai_body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
            for tool in body["tools"]
        ]
    if body.get("tool_choice"):
        choice = body["tool_choice"]
        if choice.get("type") == "auto":
            openai_body["tool_choice"] = "auto"
        elif choice.get("type") == "any":
            openai_body["tool_choice"] = "required"
        elif choice.get("type") == "tool":
            openai_body["tool_choice"] = {"type": "function", "function": {"name": choice.get("name", "")}}
    return openai_body


# ---------------------------------------------------------------------------
# T3 非流式响应转换器
# ---------------------------------------------------------------------------

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}


def map_stop_reason(finish_reason: str) -> str:
    """OpenAI finish_reason → Anthropic stop_reason；未知值归 end_turn。"""
    if finish_reason is None:
        return "end_turn"
    mapped = _STOP_REASON_MAP.get(finish_reason)
    if mapped is None:
        print(f"警告: 未知 finish_reason '{finish_reason}'，归一化为 end_turn")
        return "end_turn"
    return mapped


def _usage_to_anthropic(usage: dict, cache_usage=None) -> dict:
    """usage 转换；cache_usage=(read, creation, plain) 时注入缓存字段。

    Anthropic 语义：input_tokens 不含缓存命中/写入部分（单列上报）。
    """
    prompt_tokens = (usage or {}).get("prompt_tokens", 0) or 0
    result = {
        "input_tokens": prompt_tokens,
        "output_tokens": (usage or {}).get("completion_tokens", 0) or 0,
    }
    if cache_usage is not None:
        read, creation, _plain = cache_usage
        result["input_tokens"] = max(0, prompt_tokens - read - creation)
        result["cache_read_input_tokens"] = read
        result["cache_creation_input_tokens"] = creation
    return result


def openai_completion_to_anthropic(result: dict, client_model: str, cache_usage=None) -> dict:
    """OpenAI chat.completion → Anthropic message（非流式，纯函数）。"""
    if not isinstance(result, dict) or not result.get("choices"):
        message = (result or {}).get("error", {}).get("message") if isinstance(result, dict) else None
        raise ValueError(message or "upstream returned no choices")

    choice = result["choices"][0]
    message = choice.get("message") or {}
    content_blocks = []

    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        content_blocks.append({"type": "thinking", "thinking": reasoning})

    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            input_json = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError):
            input_json = {}
        if not isinstance(input_json, dict):
            input_json = {}
        content_blocks.append({
            "type": "tool_use",
            "id": call.get("id") or f"toolu_{int(time.time() * 1000)}",
            "name": function.get("name", ""),
            "input": input_json,
        })

    return {
        "id": result.get("id") or f"msg_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": client_model,
        "content": content_blocks,
        "stop_reason": map_stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": _usage_to_anthropic(result.get("usage"), cache_usage),
    }


# ---------------------------------------------------------------------------
# T4 流式状态机
# ---------------------------------------------------------------------------

class AnthropicStreamConverter:
    """归一化 OpenAI chunk 序列 → Anthropic SSE 事件序列。

    块类型（thinking/text/tool_use）切换时输出 content_block_stop/start 对；
    usage 在 message_start 带 input_tokens、message_delta 带累计 output_tokens。

    cache_usage=(read, creation, plain) 非空时，message_start 与 message_delta
    注入同一组缓存字段（两处口径必须一致，否则客户端按字段取 max 记账）。
    """

    def __init__(self, client_model: str, cache_usage=None):
        self._client_model = client_model
        self._message_id = f"msg_{int(time.time() * 1000)}"
        self._started = False
        self._block_index = -1
        self._block_type = None  # "thinking" | "text" | "tool_use"
        self._input_tokens = 0
        self._output_tokens = 0
        self._tool_index_by_call_index = {}
        self._pending_tool_meta = {}
        self._pending_stop_reason = "end_turn"
        self._saw_finish = False
        self._finished = False
        self._cache_usage = cache_usage

    def _cache_fields(self) -> dict:
        """缓存字段统一派生点：message_start / message_delta 共用，防口径漂移。"""
        if self._cache_usage is None:
            return {}
        read, creation, _plain = self._cache_usage
        return {
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": creation,
        }

    # -- 事件构造 -----------------------------------------------------------

    def _event(self, name: str, data: dict) -> bytes:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        return f"event: {name}\ndata: {payload}\n\n".encode("utf-8")

    def _ensure_message_start(self) -> bytes:
        if self._started:
            return b""
        self._started = True
        return self._event("message_start", {
            "type": "message_start",
            "message": {
                "id": self._message_id,
                "type": "message",
                "role": "assistant",
                "model": self._client_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": self._input_tokens,
                    "output_tokens": 0,
                    **self._cache_fields(),
                },
            },
        })

    def _open_block(self, block_type: str) -> bytes:
        self._block_index += 1
        self._block_type = block_type
        if block_type == "thinking":
            block = {"type": "thinking", "thinking": ""}
        elif block_type == "tool_use":
            # name/id 由调用方先 set_tool_call 再 open，这里给占位
            block = {"type": "tool_use", "id": "", "name": "", "input": {}}
        else:
            block = {"type": "text", "text": ""}
        start_data = {
            "type": "content_block_start",
            "index": self._block_index,
            "content_block": block,
        }
        return self._event("content_block_start", start_data)

    def _close_block(self) -> bytes:
        if self._block_type is None:
            return b""
        data = {"type": "content_block_stop", "index": self._block_index}
        self._block_type = None
        return self._event("content_block_stop", data)

    # -- 公开接口 -----------------------------------------------------------

    def set_usage(self, input_tokens: int, output_tokens: int) -> None:
        """上游 usage 到达时更新（message_start 未发出则先记下）。"""
        if input_tokens:
            self._input_tokens = input_tokens
        self._output_tokens = max(self._output_tokens, output_tokens or 0)

    def feed(self, chunk: dict) -> bytes:
        """消费一个归一化 OpenAI chunk，返回若干 SSE 事件字节。"""
        out = bytearray()
        out += self._ensure_message_start()
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            usage = chunk.get("usage")
            if usage:
                self.set_usage(
                    usage.get("prompt_tokens") or 0,
                    usage.get("completion_tokens") or 0,
                )

            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                if self._block_type != "thinking":
                    out += self._close_block()
                    out += self._open_block("thinking")
                out += self._event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._block_index,
                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                })

            content = delta.get("content")
            if isinstance(content, str) and content:
                if self._block_type != "text":
                    out += self._close_block()
                    out += self._open_block("text")
                out += self._event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._block_index,
                    "delta": {"type": "text_delta", "text": content},
                })

            for call in delta.get("tool_calls") or []:
                call_index = call.get("index", 0)
                function = call.get("function") or {}
                call_id = call.get("id") or ""
                name = function.get("name") or ""
                if call_id or name:
                    meta = self._pending_tool_meta.setdefault(call_index, {})
                    if call_id:
                        meta["id"] = call_id
                    if name:
                        meta["name"] = name
                # 首次出现该 call_index → 打开新 tool_use 块
                if call_index not in self._tool_index_by_call_index:
                    self._tool_index_by_call_index[call_index] = None
                if self._tool_index_by_call_index[call_index] is None:
                    out += self._close_block()
                    meta = self._pending_tool_meta.get(call_index, {})
                    self._block_index += 1
                    self._tool_index_by_call_index[call_index] = self._block_index
                    self._block_type = "tool_use"
                    out += self._event("content_block_start", {
                        "type": "content_block_start",
                        "index": self._block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": meta.get("id") or f"toolu_{int(time.time() * 1000)}",
                            "name": meta.get("name") or "",
                            "input": {},
                        },
                    })
                arguments = function.get("arguments") or ""
                if arguments:
                    out += self._event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": self._tool_index_by_call_index[call_index],
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    })

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                out += self._close_block()
                self._pending_stop_reason = map_stop_reason(finish_reason)
                self._saw_finish = True
        return bytes(out)

    def finish(self) -> bytes:
        """断流兜底收尾：补齐 message_delta(stop_reason) + message_stop。"""
        if self._finished:
            return b""
        self._finished = True
        out = bytearray()
        out += self._ensure_message_start()
        out += self._close_block()
        if not self._saw_finish:
            self._pending_stop_reason = "end_turn"
        out += self._event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self._pending_stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self._output_tokens, **self._cache_fields()},
        })
        out += self._event("message_stop", {"type": "message_stop"})
        return bytes(out)


# ---------------------------------------------------------------------------
# T5 怪癖修补层：上游原始字节流 → 归一化 OpenAI chunk 序列
# ---------------------------------------------------------------------------

def _normalize_chunk(chunk: dict) -> bool:
    """原地修补单个上游 chunk（content:null、other 归一）。返回是否保留。"""
    if chunk.get("error_code") or chunk.get("error_msg"):
        return False  # 业务错误由调用方单独处理
    for choice in chunk.get("choices") or []:
        reason = choice.get("finish_reason")
        if reason == "other":
            choice["finish_reason"] = "stop"
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta.get("content") is None:
            # 含 reasoning chunk：OpenAI 兼容格式中空串表达同样语义
            delta["content"] = ""
    return True


def _completion_to_delta_chunk(result: dict) -> dict:
    """普通 JSON completion（message 形态）→ 单个 delta 形态 chunk。

    stream=true 上游偶尔返回普通 JSON；状态机只消费 choices[].delta，
    需先转换，否则整条回复会被静默丢弃。多 tool_calls 补 index。
    """
    import copy

    chunk = copy.deepcopy(result)
    for choice in chunk.get("choices") or []:
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        choice["delta"] = {
            "content": message.get("content"),
            "reasoning_content": message.get("reasoning_content"),
            "tool_calls": [
                {**call, "index": index} for index, call in enumerate(tool_calls)
            ],
        }
    return chunk


def _raise_business_error(chunk: dict) -> None:
    """HTTP 200 + SSE 帧内 error_code/error_msg 业务错误 → ValueError。"""
    if chunk.get("error_code") or chunk.get("error_msg"):
        raise ValueError(
            f"{chunk.get('error_msg') or 'CodeArts upstream error'}"
            f" ({chunk.get('error_code')})"
        )


def _normalize_chunk_or_none(chunk, saw_finish_flag: list):
    """归一化单个 chunk；返回保留的 chunk 或 None。saw_finish_flag[0] 记录 finish。"""
    if not isinstance(chunk, dict):
        return None
    _raise_business_error(chunk)
    if not _normalize_chunk(chunk):
        return None
    for choice in chunk.get("choices") or []:
        if choice.get("finish_reason") is not None:
            saw_finish_flag[0] = True
    return chunk


def make_synthetic_finish_chunk(model: str) -> dict:
    """断流兜底：合成 finish chunk（同 server.py generate() 的 proxy-finish 语义）。"""
    return {
        "id": "proxy-finish",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }


def _sse_payload_iter(raw: bytes):
    """SSE 字节块 → (payload, is_done) 序列；is_done=True 表示 [DONE] 后收尾。"""
    saw_done = False
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            saw_done = True
            continue
        try:
            yield json.loads(payload.decode("utf-8")), False
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    if saw_done:
        yield None, True


def patch_stream_to_chunks(raw: bytes, model: str) -> list:
    """上游 SSE/JSON 原始字节 → 归一化 OpenAI chunk 字典列表（非增量入口，测试用）。

    修补项：stream=true 返 JSON 转 delta 形态、补缺失 finish_reason、
    content:null 归空串、other 归一化、HTTP 200 + SSE 业务错误抛 ValueError。
    """
    # stream=true 上游可能返回普通 JSON：转为 delta 形态再进状态机
    try:
        result = json.loads(raw.decode("utf-8"))
        if isinstance(result, dict) and (result.get("choices") or result.get("error")):
            if result.get("error"):
                raise ValueError(result["error"].get("message") or "upstream error")
            chunks = [_completion_to_delta_chunk(result)]
        else:
            chunks = [chunk for chunk, _ in _sse_payload_iter(raw)]
    except (UnicodeDecodeError, json.JSONDecodeError):
        chunks = [chunk for chunk, _ in _sse_payload_iter(raw)]

    saw_finish = [False]
    normalized = []
    for chunk in chunks:
        kept = _normalize_chunk_or_none(chunk, saw_finish)
        if kept is not None:
            normalized.append(kept)

    if not saw_finish[0]:
        normalized.append(make_synthetic_finish_chunk(model))
    return normalized


def patch_stream_incremental(upstream, model: str):
    """增量修补生成器：上游响应对象 → 归一化 OpenAI chunk 字典序列（真流式）。

    按 SSE 行边界增量切分，逐帧 yield；上游返回普通 JSON 时整体读完
    转 delta 形态；结束仍未见到 finish_reason 时补合成 finish chunk。
    """
    saw_finish = [False]
    buffer = b""
    json_mode = None  # None=未知；True=普通 JSON；False=SSE
    # JSON 模式 buffer 上限（异常上游慢滴防护）；真实响应受 max_tokens 约束远小于此
    _JSON_BUFFER_LIMIT = 16 * 1024 * 1024

    def _drain_sse_lines(buf: bytes):
        """消费 buffer 中全部完整 SSE 行（含残余最后一行），yield 归一化 chunk。"""
        lines = buf.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line or not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload == b"[DONE]":
                continue
            try:
                chunk = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            kept = _normalize_chunk_or_none(chunk, saw_finish)
            if kept is not None:
                yield kept

    for piece in upstream.iter_content(chunk_size=None):
        if json_mode is None:
            stripped = (buffer + piece).lstrip()
            # 普通 JSON 以 { 开头；SSE 以 data:/冒头注释/空白行开始
            if stripped.startswith(b"{"):
                json_mode = True
            elif stripped[:5] == b"data:" or b"\n" in stripped:
                json_mode = False
        if json_mode:
            buffer += piece  # JSON 模式：攒到读完再解析
            if len(buffer) > _JSON_BUFFER_LIMIT:
                raise ValueError("upstream JSON response exceeds 16MiB limit")
            continue

        buffer += piece
        *lines, buffer = buffer.split(b"\n")
        for kept in _drain_sse_lines(lines and b"\n".join(lines) or b""):
            yield kept

    # 残余 buffer 收尾：SSE 模式下最后一行可能不带结尾换行，不能丢弃
    if json_mode is False and buffer:
        for kept in _drain_sse_lines(buffer):
            yield kept
        buffer = b""

    if json_mode:
        # 普通 JSON：整体聚合为单条 completion 再转 delta 形态
        try:
            result = json.loads(buffer.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = None
        if isinstance(result, dict):
            if result.get("error"):
                raise ValueError(result["error"].get("message") or "upstream error")
            if result.get("choices") or result.get("error"):  # 统一两入口判定口径
                kept = _normalize_chunk_or_none(_completion_to_delta_chunk(result), saw_finish)
                if kept is not None:
                    yield kept

    if not saw_finish[0]:
        yield make_synthetic_finish_chunk(model)


# ---------------------------------------------------------------------------
# T5 蓝图路由
# ---------------------------------------------------------------------------

blueprint = Blueprint("anthropic_proxy", __name__)


@blueprint.route("/v1/messages", methods=["POST", "OPTIONS"])
def messages():
    if request.method == "OPTIONS":
        # 无 CORS 头：CLI 客户端不发预检；server.py 全局 after_request 的既有
        # CORS 策略不在本次范围内调整。
        return Response("", status=204)

    body_bytes = request.get_data()
    if not body_bytes:
        return _anthropic_error("empty body")
    try:
        body = json.loads(body_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _anthropic_error("invalid JSON body")
    if not isinstance(body, dict):
        return _anthropic_error("invalid JSON body")

    refresh_models_if_needed_fn()
    supported = get_supported_models()

    # 模型映射（三步法），随后按 OpenAI 端点同一套规则校验
    client_model = body.get("model") or ""
    if not isinstance(client_model, str) or not client_model:
        return _anthropic_error("model is required")
    model = resolve_model(client_model)
    if model not in supported:
        return _anthropic_error(
            f"model '{client_model}' (resolved to '{model}') 不在当前账号可用模型列表中",
            error_type="not_found_error",
            status=404,
        )
    detail = get_model_details().get(model, {})
    supports_images = bool(detail.get("supports_images", False))

    # 请求翻译前校验 image block 合法性（不发上游）
    if not supports_images:
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "image":
                        return _anthropic_error(
                            f"model '{model}' 不支持图片输入，请移除 image block",
                        )

    openai_body = anthropic_messages_to_openai(body, resolve_model)
    want_stream = bool(openai_body.get("stream", False))
    benefit = model not in AGENT_MODELS
    payload = json.dumps(openai_body, ensure_ascii=False).encode("utf-8")
    headers = signed_headers_fn(payload, benefit=benefit, model_id=model)

    try:
        upstream = requests.post(TARGET_URL, data=payload, headers=headers, stream=True, timeout=600)
    except Exception as error:
        return _anthropic_error(f"upstream error: {error}", error_type="api_error", status=502)

    if upstream.status_code != 200:
        try:
            detail_text = upstream.text[:2000]
        except Exception:
            detail_text = ""
        return _anthropic_error(
            f"upstream {upstream.status_code}: {detail_text}",
            error_type="api_error",
            status=502,
        )

    if want_stream:
        # 流式：真实 usage 要等流结束才到，缓存注入值在流开始前用本地估算作 total 基准；
        # commit=False 只查询指纹，流正常收尾后再回写（失败请求不污染指纹表）
        cache_usage = cache_sim.select_cache_usage(
            cache_sim.estimate_body_tokens(body), body, commit=False
        )
        converter = AnthropicStreamConverter(client_model, cache_usage=cache_usage)

        def _sse_error_event(message: str) -> bytes:
            """流中异常 → 规范 SSE error 帧（裸 JSON 会被 SSE 解析器丢弃）。"""
            payload = json.dumps(
                {"type": "error", "error": {"type": "api_error", "message": message}},
                ensure_ascii=False,
            )
            return f"event: error\ndata: {payload}\n\n".encode("utf-8")

        def generate():
            try:
                for chunk in patch_stream_incremental(upstream, model):
                    events = converter.feed(chunk)
                    if events:
                        yield events
                yield converter.finish()
                # 流正常收尾才回写指纹表（error 帧路径不经过这里）
                cache_sim.tracker.update("default", cache_sim.build_profile(body))
            except GeneratorExit:
                # 客户端中途断开：释放上游连接（GeneratorExit 不走 except Exception）
                upstream.close()
                raise
            except ValueError as error:
                # 流式路径 HTTP 200 + 业务错误 → Anthropic error 事件
                print(f"警告: 上游业务错误: {error}")
                yield _sse_error_event(str(error))
            except Exception as error:  # noqa: BLE001
                print(f"警告: 流式转换异常: {error!r}")
                yield _sse_error_event(f"stream error: {error}")

        resp = Response(generate(), status=200, mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache, no-transform"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    # 非流式：统一聚合（上游可能错误地返回 SSE）
    result = parse_upstream_json_fn(upstream)
    if isinstance(result, dict) and result.get("error"):
        message = result["error"].get("message") or "CodeArts upstream error"
        return _anthropic_error(message, error_type="api_error", status=502)
    try:
        # 非流式：以上游真实 prompt_tokens 作 total 基准计算缓存派生；
        # 转换成功（上游结果已确认可用）后才回写指纹表
        real_total = ((result or {}).get("usage") or {}).get("prompt_tokens") or 0
        cache_usage = cache_sim.select_cache_usage(real_total, body, commit=False)
        anthropic_message = openai_completion_to_anthropic(
            result, client_model, cache_usage=cache_usage
        )
        cache_sim.tracker.update("default", cache_sim.build_profile(body))
    except ValueError as error:
        return _anthropic_error(str(error), error_type="api_error", status=502)
    return Response(
        json.dumps(anthropic_message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status=200,
        content_type="application/json; charset=utf-8",
    )


@blueprint.route("/v1/messages/count_tokens", methods=["POST", "OPTIONS"])
def count_tokens():
    """粗略估算 token 数（本地估算，不发上游）；Claude Code 偶尔会调用。"""
    if request.method == "OPTIONS":
        return Response("", status=204)
    body_bytes = request.get_data()
    try:
        body = json.loads(body_bytes or b"{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _anthropic_error("invalid JSON body")
    if not isinstance(body, dict):
        return _anthropic_error("invalid JSON body")
    total_chars = 0
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(block.get("text", "") or "")
                    total_chars += len(json.dumps(block.get("input") or {}, ensure_ascii=False))
    system = body.get("system")
    if isinstance(system, str):
        total_chars += len(system)
    elif isinstance(system, list):
        total_chars += sum(len(b.get("text", "") or "") for b in system if isinstance(b, dict))
    estimate = max(1, total_chars // 4)
    return Response(
        json.dumps({"input_tokens": estimate}).encode("utf-8"),
        status=200,
        content_type="application/json",
    )
