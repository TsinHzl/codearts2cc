# -*- coding: utf-8 -*-
"""anthropic_proxy 单元测试（T1-T4 转换器，纯逻辑不发真实请求）。"""
import json

import pytest
from flask import Flask

import anthropic_proxy
import config_store


# ---------------------------------------------------------------------------
# 依赖注入 stub
# ---------------------------------------------------------------------------

@pytest.fixture
def injected(monkeypatch):
    """注入假依赖：3 个 agent 模型 + 2 个 benefit 模型。"""
    agent_models = ["openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"]
    supported = agent_models + ["glm-5.3-flash", "deepseek-v4-pro-0813"]
    details = {model_id: {"supports_images": False} for model_id in supported}

    monkeypatch.setattr(anthropic_proxy, "AGENT_MODELS", agent_models)
    monkeypatch.setattr(anthropic_proxy, "get_supported_models", lambda: supported)
    monkeypatch.setattr(anthropic_proxy, "get_model_details", lambda: details)
    monkeypatch.setattr(
        anthropic_proxy, "refresh_models_if_needed_fn", lambda: None
    )
    monkeypatch.setattr(
        anthropic_proxy, "signed_headers_fn",
        lambda body, benefit, model_id: {"x-test": "1"},
    )
    monkeypatch.setattr(
        anthropic_proxy, "parse_upstream_json_fn",
        lambda upstream: json.loads(upstream.text),
    )
    return {"agent_models": agent_models, "supported": supported, "details": details}


@pytest.fixture
def client(injected):
    app = Flask(__name__)
    app.register_blueprint(anthropic_proxy.blueprint)
    app.testing = True
    return app.test_client()


# ---------------------------------------------------------------------------
# T1 请求转换器
# ---------------------------------------------------------------------------

class TestRequestConverter:
    def test_plain_text(self):
        """纯文本：system string + user text → system/user 消息。"""
        body = {
            "model": "openpangu-2.0-pro",
            "system": "你是一个助手",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "你好"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["messages"][0] == {"role": "system", "content": "你是一个助手"}
        assert result["messages"][1] == {"role": "user", "content": "你好"}
        assert result["max_tokens"] == 100
        assert result["stream"] is False

    def test_with_tools_and_tool_result(self):
        """带工具：tools 转格式；tool_use → assistant.tool_calls；tool_result → 独立 tool 消息。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "tools": [{
                "name": "echo",
                "description": "回声",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
            }],
            "messages": [
                {"role": "user", "content": "运行 echo"},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "echo", "input": {"text": "hi"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "hi"},
                ]},
            ],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["tools"][0]["type"] == "function"
        assert result["tools"][0]["function"]["name"] == "echo"
        assert result["messages"][1]["tool_calls"][0]["id"] == "toolu_1"
        assert json.loads(result["messages"][1]["tool_calls"][0]["function"]["arguments"]) == {"text": "hi"}
        tool_message = result["messages"][2]
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "toolu_1"
        assert tool_message["content"] == "hi"

    def test_multi_turn_alternating(self):
        """多轮交替：system blocks 形态 + 多轮 user/assistant 文本。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
            "messages": [
                {"role": "user", "content": "问题一"},
                {"role": "assistant", "content": [{"type": "text", "text": "回答一"}]},
                {"role": "user", "content": "问题二"},
            ],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["messages"][0]["role"] == "system"
        assert "第一段" in result["messages"][0]["content"]
        assert [m["role"] for m in result["messages"][1:]] == ["user", "assistant", "user"]

    def test_image_rejected_without_support(self, client, monkeypatch):
        """图片拒绝：supports_images=false 收 image block → 400，不发上游。"""
        body = {
            "model": "glm-5.3-flash",
            "max_tokens": 100,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGk="}},
                    {"type": "text", "text": "这是什么"},
                ],
            }],
        }

        def fake_post(*args, **kwargs):
            raise AssertionError("不应发上游")

        monkeypatch.setattr(anthropic_proxy.requests, "post", fake_post)
        resp = client.post("/v1/messages", json=body)
        assert resp.status_code == 400
        payload = resp.get_json()
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "invalid_request_error"

    def test_mixed_blocks_tool_first_text_later(self):
        """混合块排序：tool_result 与 text 同 turn → tool 消息在前、text 合并垫底。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_a", "name": "f1", "input": {}},
                    {"type": "tool_use", "id": "toolu_b", "name": "f2", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "text", "text": "附注"},
                    {"type": "tool_result", "tool_use_id": "toolu_a", "content": "结果A"},
                    {"type": "tool_result", "tool_use_id": "toolu_b", "content": "结果B"},
                ]},
            ],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        roles = [m["role"] for m in result["messages"]]
        assert roles == ["assistant", "tool", "tool", "user"]
        assert result["messages"][1]["tool_call_id"] == "toolu_a"
        assert result["messages"][2]["tool_call_id"] == "toolu_b"
        assert result["messages"][3]["content"] == "附注"

    def test_image_media_type_fallback(self):
        """image 块 media_type 缺失时兜底 image/png，不产生 data:None 畸形 URI。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "data": "aGk="}},  # 无 media_type
                    {"type": "text", "text": "这是什么"},
                ],
            }],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        urls = [
            part["image_url"]["url"]
            for m in result["messages"]
            if isinstance(m.get("content"), list)
            for part in m["content"]
            if isinstance(part, dict) and part.get("type") == "image_url"
        ]
        assert urls == ["data:image/png;base64,aGk="]


# ---------------------------------------------------------------------------
# T2 模型映射
# ---------------------------------------------------------------------------

class TestResolveModel:
    def test_context_suffix_stripped(self, monkeypatch):
        """[4m] 后缀剥离后命中默认映射表。"""
        monkeypatch.setattr(config_store, "_data", {})
        assert anthropic_proxy.resolve_model("claude-sonnet-4-5[4m]") == "openpangu-2.0-pro"

    def test_env_alias_hit(self, monkeypatch):
        """config.json 别名表覆盖默认表。"""
        monkeypatch.setattr(config_store, "_data",
                            {"CODEARTS_ANTHROPIC_MODEL_MAP": "my-alias:GLM-5.2"})
        assert anthropic_proxy.resolve_model("my-alias") == "GLM-5.2"

    def test_claude_fallback_to_first_supported(self, monkeypatch, injected):
        """claude-* 未命中兜底 SUPPORTED_MODELS[0]。"""
        monkeypatch.setattr(config_store, "_data", {})
        assert anthropic_proxy.resolve_model("claude-opus-9-9") == injected["supported"][0]

    def test_non_claude_passthrough(self, monkeypatch):
        """非 claude-* 名称原样透传，交由路由校验。"""
        monkeypatch.setattr(config_store, "_data", {})
        assert anthropic_proxy.resolve_model("qwen3-max") == "qwen3-max"


# ---------------------------------------------------------------------------
# T3 非流式响应转换器
# ---------------------------------------------------------------------------

class TestCompletionConverter:
    def test_plain_text(self):
        result = {
            "id": "chatcmpl-1", "model": "openpangu-2.0-pro",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "你好"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        message = anthropic_proxy.openai_completion_to_anthropic(result, "claude-sonnet-4-5")
        assert message["type"] == "message"
        assert message["model"] == "claude-sonnet-4-5"
        assert message["content"] == [{"type": "text", "text": "你好"}]
        assert message["stop_reason"] == "end_turn"
        assert message["usage"] == {"input_tokens": 10, "output_tokens": 5}

    def test_tool_calls(self):
        result = {
            "id": "chatcmpl-2", "model": "openpangu-2.0-pro",
            "choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": "call_1", "type": "function",
                                                     "function": {"name": "echo", "arguments": "{\"text\":\"hi\"}"}}]}}],
        }
        message = anthropic_proxy.openai_completion_to_anthropic(result, "claude-sonnet-4-5")
        assert message["stop_reason"] == "tool_use"
        assert message["content"][0] == {
            "type": "tool_use", "id": "call_1", "name": "echo", "input": {"text": "hi"},
        }

    def test_upstream_error_raises(self):
        with pytest.raises(ValueError):
            anthropic_proxy.openai_completion_to_anthropic(
                {"error": {"message": "boom"}}, "claude-sonnet-4-5"
            )


# ---------------------------------------------------------------------------
# T4 流式状态机
# ---------------------------------------------------------------------------

def _parse_events(raw: bytes) -> list:
    events = []
    for block in raw.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_type = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_type = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event_type:
            events.append((event_type, data))
    return events


class TestStreamConverter:
    def test_text_stream(self):
        converter = anthropic_proxy.AnthropicStreamConverter("claude-sonnet-4-5")
        raw = converter.feed({"choices": [{"delta": {"content": "你"}}]})
        raw += converter.feed({"choices": [{"delta": {"content": "好"},
                                            "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 3, "completion_tokens": 2}})
        raw += converter.finish()
        events = _parse_events(raw)
        names = [name for name, _ in events]
        assert names[0] == "message_start"
        assert names[1] == "content_block_start"
        assert events[1][1]["content_block"]["type"] == "text"
        assert "content_block_delta" in names
        assert names[-2:] == ["message_delta", "message_stop"]
        message_delta = events[-2][1]
        assert message_delta["delta"]["stop_reason"] == "end_turn"
        assert message_delta["usage"]["output_tokens"] == 2
        stop_index = names.index("content_block_stop")
        assert stop_index < names.index("message_delta")

    def test_tool_call_stream(self):
        converter = anthropic_proxy.AnthropicStreamConverter("claude-sonnet-4-5")
        raw = converter.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "echo", "arguments": ""}},
        ]}}]})
        raw += converter.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"text\":"}},
        ]}}]})
        raw += converter.feed({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"hi\"}"}},
        ]}, "finish_reason": "tool_calls"}]})
        raw += converter.finish()
        events = _parse_events(raw)
        start = next(data for name, data in events if name == "content_block_start")
        assert start["content_block"] == {"type": "tool_use", "id": "call_1", "name": "echo", "input": {}}
        partial_json = "".join(
            data["delta"]["partial_json"] for name, data in events
            if name == "content_block_delta"
        )
        assert json.loads(partial_json) == {"text": "hi"}
        message_delta = next(data for name, data in events if name == "message_delta")
        assert message_delta["delta"]["stop_reason"] == "tool_use"

    def test_thinking_then_text(self):
        converter = anthropic_proxy.AnthropicStreamConverter("claude-sonnet-4-5")
        raw = converter.feed({"choices": [{"delta": {"content": None, "reasoning_content": "思考中"}}]})
        raw += converter.feed({"choices": [{"delta": {"content": "答案"},
                                            "finish_reason": "stop"}]})
        raw += converter.finish()
        events = _parse_events(raw)
        starts = [(name, data) for name, data in events if name == "content_block_start"]
        assert starts[0][1]["content_block"]["type"] == "thinking"
        assert starts[1][1]["content_block"]["type"] == "text"
        thinking_delta = next(data for name, data in events
                              if name == "content_block_delta" and data["delta"]["type"] == "thinking_delta")
        assert thinking_delta["delta"]["thinking"] == "思考中"
        # thinking 块切换到 text 前必须先 close
        names = [name for name, _ in events]
        first_thinking_stop = names.index("content_block_stop")
        text_start = next(i for i, (name, data) in enumerate(events) if name == "content_block_start"
                          and data["content_block"]["type"] == "text")
        assert first_thinking_stop < text_start

    def test_truncated_stream_finish_fallback(self):
        """断流兜底：无 finish chunk 直接 finish() → 补 end_turn + message_stop。"""
        converter = anthropic_proxy.AnthropicStreamConverter("claude-sonnet-4-5")
        raw = converter.feed({"choices": [{"delta": {"content": "半句"}}]})
        raw += converter.finish()
        events = _parse_events(raw)
        names = [name for name, _ in events]
        assert names[-2:] == ["message_delta", "message_stop"]
        message_delta = events[-2][1]
        assert message_delta["delta"]["stop_reason"] == "end_turn"


# ---------------------------------------------------------------------------
# T5 请求体结构校验（C1/C2 回归）
# ---------------------------------------------------------------------------

class TestBodyValidation:
    def test_messages_non_dict_element_rejected(self, client):
        """messages 元素非 dict（如字符串）→ 400 invalid_request_error，而非 500。"""
        body = {"model": "claude-sonnet-4-5", "max_tokens": 100, "messages": ["你好"]}
        resp = client.post("/v1/messages", json=body)
        assert resp.status_code == 400
        payload = resp.get_json()
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "invalid_request_error"

    def test_tool_choice_string_rejected(self, client):
        """tool_choice 字符串形态（"auto"）→ 400，而非 500。"""
        body = {
            "model": "claude-sonnet-4-5",
            "max_tokens": 100,
            "tool_choice": "auto",
            "messages": [{"role": "user", "content": "你好"}],
        }
        resp = client.post("/v1/messages", json=body)
        assert resp.status_code == 400
        assert resp.get_json()["error"]["type"] == "invalid_request_error"

    def test_count_tokens_malformed_messages_rejected(self, client):
        """count_tokens：messages 含非 dict 元素 → 400，而非 500。"""
        resp = client.post("/v1/messages/count_tokens", json={"messages": ["hi"]})
        assert resp.status_code == 400
        assert resp.get_json()["type"] == "error"

    def test_scalar_content_rejected(self, client):
        """message content 为标量（如 5）→ 400，而非 500。"""
        body = {
            "model": "claude-sonnet-4-5", "max_tokens": 100,
            "messages": [{"role": "user", "content": 5}],
        }
        resp = client.post("/v1/messages", json=body)
        assert resp.status_code == 400
        assert resp.get_json()["error"]["type"] == "invalid_request_error"

    def test_non_list_tools_rejected(self, client):
        """tools 为字符串 → 400，而非 500。"""
        body = {
            "model": "claude-sonnet-4-5", "max_tokens": 100,
            "tools": "oops",
            "messages": [{"role": "user", "content": "你好"}],
        }
        resp = client.post("/v1/messages", json=body)
        assert resp.status_code == 400
        assert resp.get_json()["error"]["type"] == "invalid_request_error"


# ---------------------------------------------------------------------------
# 使用日志埋点（端到端：mock 上游，验证成功路径写入、失败路径不写）
# ---------------------------------------------------------------------------

class _FakeStreamResponse:
    """模拟上游 SSE 响应（Content-Type 为 event-stream）。"""

    def __init__(self, lines):
        self._lines = lines
        self.headers = {"Content-Type": "text/event-stream"}
        self.status_code = 200

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)

    def iter_content(self, chunk_size=None):
        # patch_stream_incremental 走原始字节流接口，按行拼接换行还原
        for line in self._lines:
            yield line + b"\n"

    def close(self):
        pass


class _FakeJsonResponse:
    """模拟上游误回 JSON 的响应。"""

    def __init__(self, payload):
        self.headers = {"Content-Type": "application/json"}
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class TestUsageRecording:
    @pytest.fixture(autouse=True)
    def _no_cache_sim(self, monkeypatch):
        """关闭 cache 模拟，避免跨测试指纹表污染 input_tokens 拆分。"""
        monkeypatch.setattr(config_store, "_data", {"CODEARTS_CACHE_SIM": "0"})

    def test_stream_success_records_usage(self, client, monkeypatch, tmp_path):
        """流式成功收尾 → record 被调用一次，含 client_ip 与 usage。"""
        import usage_log
        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(tmp_path))
        monkeypatch.setattr(anthropic_proxy, "record_usage_fn", usage_log.record)
        try:
            sse_lines = [
                b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
                b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
                b"data: [DONE]",
            ]
            monkeypatch.setattr(
                anthropic_proxy.requests, "post",
                lambda *a, **kw: _FakeStreamResponse(sse_lines),
            )
            body = {"model": "GLM-5.2", "max_tokens": 10, "stream": True,
                    "messages": [{"role": "user", "content": "你好"}]}
            resp = client.post("/v1/messages", json=body, environ_base={"REMOTE_ADDR": "9.9.9.9"})
            resp.get_data()  # 强制完整消费流式响应，触发 generator 收尾的 record
            assert resp.status_code == 200
            data = usage_log.query()
            assert data["summary"]["requests"] == 1
            entry = data["entries"][0]
            assert entry["endpoint"] == "anthropic"
            assert entry["ip"] == "9.9.9.9"
            assert entry["stream"] is True
            assert entry["output_tokens"] == 2
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)

    def test_client_disconnect_not_recorded(self, client, monkeypatch, tmp_path):
        """客户端中途断开（GeneratorExit）→ 不写使用日志。"""
        import usage_log

        class _DisconnectResponse(_FakeStreamResponse):
            def iter_lines(self, decode_unicode=False):
                yield b'data: {"choices":[{"delta":{"content":"a"}}]}'
                raise GeneratorExit("client gone")

        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(tmp_path))
        monkeypatch.setattr(anthropic_proxy, "record_usage_fn", usage_log.record)
        try:
            monkeypatch.setattr(
                anthropic_proxy.requests, "post",
                lambda *a, **kw: _DisconnectResponse([]),
            )
            body = {"model": "GLM-5.2", "max_tokens": 10, "stream": True,
                    "messages": [{"role": "user", "content": "你好"}]}
            resp = client.post("/v1/messages", json=body)
            assert resp.status_code == 200
            assert usage_log.query()["summary"]["requests"] == 0
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)

    def test_stream_business_error_not_recorded(self, client, monkeypatch, tmp_path):
        """HTTP 200 + SSE 业务错误（ValueError 路径）→ 不写使用日志。"""
        import usage_log
        sse_lines = [
            b'data: {"error_code":"ModelNotRegistered","error_msg":"model is not registered"}',
        ]
        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(tmp_path))
        monkeypatch.setattr(anthropic_proxy, "record_usage_fn", usage_log.record)
        try:
            monkeypatch.setattr(
                anthropic_proxy.requests, "post",
                lambda *a, **kw: _FakeStreamResponse(sse_lines),
            )
            body = {"model": "GLM-5.2", "max_tokens": 10, "stream": True,
                    "messages": [{"role": "user", "content": "你好"}]}
            resp = client.post("/v1/messages", json=body)
            assert resp.status_code == 200
            assert b'"type":"error"' in resp.data or b'"type": "error"' in resp.data
            assert usage_log.query()["summary"]["requests"] == 0
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)

    def test_non_stream_success_records_usage(self, client, monkeypatch, tmp_path):
        """非流式成功 → record 一次，stream=False、token 数正确。"""
        import usage_log
        payload = {
            "choices": [{"message": {"role": "assistant", "content": "ok"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1},
        }
        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(tmp_path))
        monkeypatch.setattr(anthropic_proxy, "record_usage_fn", usage_log.record)
        try:
            monkeypatch.setattr(
                anthropic_proxy.requests, "post",
                lambda *a, **kw: _FakeJsonResponse(payload),
            )
            body = {"model": "GLM-5.2", "max_tokens": 10,
                    "messages": [{"role": "user", "content": "你好"}]}
            resp = client.post("/v1/messages", json=body)
            assert resp.status_code == 200
            data = usage_log.query()
            assert data["summary"]["requests"] == 1
            entry = data["entries"][0]
            assert entry["stream"] is False
            assert entry["input_tokens"] == 7
            assert entry["output_tokens"] == 1
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)

    def test_upstream_502_not_recorded(self, client, monkeypatch, tmp_path):
        """上游非 200 → 502，不写使用日志。"""
        import usage_log

        class _BadResponse:
            headers = {"Content-Type": "application/json"}
            status_code = 500
            text = "boom"

        monkeypatch.setattr(usage_log, "_initialized", False)
        usage_log.init(str(tmp_path))
        monkeypatch.setattr(anthropic_proxy, "record_usage_fn", usage_log.record)
        try:
            monkeypatch.setattr(
                anthropic_proxy.requests, "post",
                lambda *a, **kw: _BadResponse(),
            )
            body = {"model": "GLM-5.2", "max_tokens": 10,
                    "messages": [{"role": "user", "content": "你好"}]}
            resp = client.post("/v1/messages", json=body)
            assert resp.status_code == 502
            assert usage_log.query()["summary"]["requests"] == 0
        finally:
            monkeypatch.setattr(usage_log, "_initialized", False)
