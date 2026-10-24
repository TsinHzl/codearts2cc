# -*- coding: utf-8 -*-
"""anthropic_proxy 单元测试（T1-T4 转换器，纯逻辑不发真实请求）。"""
import json
import time

import pytest
from flask import Flask

from src.config import config_store
from src.proxy import anthropic_proxy


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

    def test_effort_passthrough_with_value(self):
        """自带 thinking.effort → 原样透传到 reasoning_effort。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "thinking": {"type": "enabled", "effort": "low"},
            "messages": [{"role": "user", "content": "问题"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["reasoning_effort"] == "low"

    def test_effort_default_high_when_missing(self):
        """缺省 thinking → reasoning_effort 补默认 "high"。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "问题"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["reasoning_effort"] == "high"

    def test_effort_default_high_when_thinking_not_dict(self):
        """thinking 非 dict → 不抛异常，reasoning_effort 补默认 "high"。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "thinking": "enabled",
            "messages": [{"role": "user", "content": "问题"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["reasoning_effort"] == "high"

    def test_effort_empty_string_falls_back_high(self):
        """effort 空串 → 补默认 "high"（透传优先仅对非空字符串生效）。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "thinking": {"type": "enabled", "effort": ""},
            "messages": [{"role": "user", "content": "问题"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["reasoning_effort"] == "high"

    def test_effort_non_string_falls_back_high(self):
        """effort 非字符串（如 int）→ 补默认 "high"。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "thinking": {"type": "enabled", "effort": 5},
            "messages": [{"role": "user", "content": "问题"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert result["reasoning_effort"] == "high"

    def test_effort_disabled_omits_field(self):
        """thinking.type=="disabled" → 尊重客户端意图，不发 reasoning_effort 字段。"""
        body = {
            "model": "openpangu-2.0-pro",
            "max_tokens": 100,
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "问题"}],
        }
        result = anthropic_proxy.anthropic_messages_to_openai(body, lambda m: m)
        assert "reasoning_effort" not in result

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

    def test_non_stream_thinking_as_text_block(self):
        result = {
            "id": "chatcmpl-3", "model": "openpangu-2.0-pro",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "答案",
                                     "reasoning_content": "想一想\n再想想"}}],
        }
        message = anthropic_proxy.openai_completion_to_anthropic(
            result, "claude-sonnet-4-5", thinking_as_text=True
        )
        assert message["content"][0] == {
            "type": "text",
            "text": (
                f"{anthropic_proxy.THINKING_TEXT_HEADER_LINE}\n"
                f"{anthropic_proxy.ANSI_DIM}想一想{anthropic_proxy.ANSI_RESET}\n"
                f"{anthropic_proxy.ANSI_DIM}再想想{anthropic_proxy.ANSI_RESET}"
            ),
        }
        assert message["content"][1] == {"type": "text", "text": "答案"}
        assert "Thought for" not in message["content"][0]["text"]

    def test_non_stream_thinking_as_text_not_limited(self):
        huge = "行\n" * (anthropic_proxy.MAX_THINKING_TEXT_BLOCK_BYTES // 2)
        result = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "答案",
                                     "reasoning_content": huge}}],
        }
        message = anthropic_proxy.openai_completion_to_anthropic(
            result, "claude-sonnet-4-5", thinking_as_text=True
        )
        assert message["content"][0]["type"] == "text"
        # 非流式不限额：所有行完整输出（ANSI 包裹使 len 恒大于原始字节，不能用作判据）
        assert message["content"][0]["text"].count("行") == (
            anthropic_proxy.MAX_THINKING_TEXT_BLOCK_BYTES // 2
        )

    def test_non_stream_thinking_as_text_off_keeps_thinking(self):
        result = {
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "答案",
                                     "reasoning_content": "原生"}}],
        }
        message = anthropic_proxy.openai_completion_to_anthropic(result, "claude-sonnet-4-5")
        assert message["content"][0] == {"type": "thinking", "thinking": "原生"}


# ---------------------------------------------------------------------------
# T3.5 thinking 文本化（借鉴 cc-proxy src/thinking-text.js）
# ---------------------------------------------------------------------------

class TestThinkingTextFormatting:
    def test_format_thinking_header_and_lines(self):
        """多行思考：头行 + 每行独立 dim 包裹，行间以 \\n 连接。"""
        text = anthropic_proxy.format_thinking_as_text("第一行\n第二行")
        assert text == (
            f"{anthropic_proxy.ANSI_DIM}💭 Thinking{anthropic_proxy.ANSI_RESET}\n"
            f"{anthropic_proxy.ANSI_DIM}第一行{anthropic_proxy.ANSI_RESET}\n"
            f"{anthropic_proxy.ANSI_DIM}第二行{anthropic_proxy.ANSI_RESET}"
        )

    def test_format_thinking_skips_blank_lines_and_trims(self):
        """空行跳过、每行 strip（缩进丢失，与 cc-proxy 一致）。"""
        text = anthropic_proxy.format_thinking_as_text("  缩进\n\n  \n尾行  ")
        assert text.split("\n") == [
            f"{anthropic_proxy.ANSI_DIM}💭 Thinking{anthropic_proxy.ANSI_RESET}",
            f"{anthropic_proxy.ANSI_DIM}缩进{anthropic_proxy.ANSI_RESET}",
            f"{anthropic_proxy.ANSI_DIM}尾行{anthropic_proxy.ANSI_RESET}",
        ]

    def test_format_thinking_crlf_normalized(self):
        text = anthropic_proxy.format_thinking_as_text("a\r\nb\r")
        assert "\r" not in text
        assert text.endswith(f"{anthropic_proxy.ANSI_DIM}b{anthropic_proxy.ANSI_RESET}")

    def test_format_thinking_blank_only_returns_header(self):
        assert anthropic_proxy.format_thinking_as_text("  \n\n ") == \
            anthropic_proxy.THINKING_TEXT_HEADER_LINE

    def test_format_thinking_empty_returns_none(self):
        assert anthropic_proxy.format_thinking_as_text("") is None
        assert anthropic_proxy.format_thinking_as_text(None) is None

    def test_format_thinking_strips_csi(self):
        text = anthropic_proxy.format_thinking_as_text("前\x1b[31m后")
        assert "\x1b[31m" not in text
        assert f"{anthropic_proxy.ANSI_DIM}前后{anthropic_proxy.ANSI_RESET}" in text

    def test_format_thinking_strips_osc_whole(self):
        """OSC 必须整体剥离：ESC ] 与 payload 都不能残留（顺序错误会漏 payload）。"""
        text = anthropic_proxy.format_thinking_as_text("a\x1b]0;title\x07b")
        assert "title" not in text
        assert f"{anthropic_proxy.ANSI_DIM}ab{anthropic_proxy.ANSI_RESET}" in text

    def test_format_thinking_strips_two_char_escape(self):
        text = anthropic_proxy.format_thinking_as_text("a\x1bMb")
        assert f"{anthropic_proxy.ANSI_DIM}ab{anthropic_proxy.ANSI_RESET}" in text

    def test_format_duration_seconds(self):
        assert anthropic_proxy.format_duration_line(12.3) == (
            f"{anthropic_proxy.ANSI_DIM}💭 Thought for 12s{anthropic_proxy.ANSI_RESET}"
        )

    def test_format_duration_minutes(self):
        assert anthropic_proxy.format_duration_line(76.4) == (
            f"{anthropic_proxy.ANSI_DIM}💭 Thought for 1m16s{anthropic_proxy.ANSI_RESET}"
        )

    def test_format_thinking_strips_osc_st_terminated(self):
        """OSC 以 ST（ESC \\）终止时同样整体剥离。"""
        text = anthropic_proxy.format_thinking_as_text("a\x1b]0;title\x1b\\b")
        assert "title" not in text
        assert f"{anthropic_proxy.ANSI_DIM}ab{anthropic_proxy.ANSI_RESET}" in text

    def test_format_thinking_strips_bare_esc(self):
        """行末裸 ESC（不完整序列）必须被吞掉，不得泄漏到输出。"""
        text = anthropic_proxy.format_thinking_as_text("残留\x1b")
        assert "\x1b" not in text.replace(anthropic_proxy.ANSI_DIM, "").replace(
            anthropic_proxy.ANSI_RESET, ""
        )
        assert f"{anthropic_proxy.ANSI_DIM}残留{anthropic_proxy.ANSI_RESET}" in text

    def test_format_thinking_strips_bare_control_chars(self):
        """BEL/BS/NUL 等裸控制字符必须被吞掉（无 ESC 前缀也不得泄漏）。"""
        text = anthropic_proxy.format_thinking_as_text("a\x07b\x08c\x00d")
        assert f"{anthropic_proxy.ANSI_DIM}abcd{anthropic_proxy.ANSI_RESET}" in text

    def test_format_thinking_keeps_tab_and_newline(self):
        """控制字符净化不得破坏 tab 与换行（行结构依赖 \\n）。"""
        text = anthropic_proxy.format_thinking_as_text("a\tb\nc")
        assert f"{anthropic_proxy.ANSI_DIM}a\tb{anthropic_proxy.ANSI_RESET}" in text
        assert f"{anthropic_proxy.ANSI_DIM}c{anthropic_proxy.ANSI_RESET}" in text

    def test_format_duration_rounds_half_up(self):
        """对齐 JS Math.round：2.5 → 3s（Python round() 银行家舍入会得 2）。"""
        assert anthropic_proxy.format_duration_line(2.5) == (
            f"{anthropic_proxy.ANSI_DIM}💭 Thought for 3s{anthropic_proxy.ANSI_RESET}"
        )
        assert anthropic_proxy.format_duration_line(0.4) == (
            f"{anthropic_proxy.ANSI_DIM}💭 Thought for 1s{anthropic_proxy.ANSI_RESET}"
        )


class TestUserAgentGate:
    @pytest.mark.parametrize("ua", [
        "claude-cli/1.0.0",
        "claude-code/2.0",
        "Claude-CLI/1.0",
        "claude-cli 1.0",
        "claude-cli",
    ])
    def test_matches(self, ua):
        assert anthropic_proxy.is_claude_code_user_agent(ua) is True

    @pytest.mark.parametrize("ua", ["curl/8.0", "my-claude-cli/1.0", "claude-coder/1.0", ""])
    def test_not_matches(self, ua):
        assert anthropic_proxy.is_claude_code_user_agent(ua) is False

    def test_none(self):
        assert anthropic_proxy.is_claude_code_user_agent(None) is False


class TestStripThinkingTextHistory:
    @staticmethod
    def _thinking_block(*lines):
        header = anthropic_proxy.THINKING_TEXT_HEADER_LINE
        body = "\n".join(
            f"{anthropic_proxy.ANSI_DIM}{line}{anthropic_proxy.ANSI_RESET}" for line in lines
        )
        return f"{header}\n{body}"

    def test_string_content(self):
        text = self._thinking_block("想") + "\n回答"
        messages = [{"role": "assistant", "content": text}]
        result = anthropic_proxy.strip_thinking_text_from_messages(messages)
        assert result[0]["content"] == "\n回答"
        # 原消息未被修改（不可变）
        assert messages[0]["content"] == text

    def test_block_content_keeps_answer(self):
        messages = [{"role": "assistant", "content": [
            {"type": "text", "text": self._thinking_block("想")},
            {"type": "text", "text": "回答"},
        ]}]
        result = anthropic_proxy.strip_thinking_text_from_messages(messages)
        assert result[0]["content"] == [{"type": "text", "text": "回答"}]

    def test_same_block_thinking_and_answer(self):
        messages = [{"role": "assistant", "content": [
            {"type": "text", "text": self._thinking_block("想") + "\n回答"},
        ]}]
        result = anthropic_proxy.strip_thinking_text_from_messages(messages)
        assert result[0]["content"] == [{"type": "text", "text": "\n回答"}]

    def test_duration_line_removed(self):
        text = (
            self._thinking_block("想") + "\n"
            + f"{anthropic_proxy.ANSI_DIM}💭 Thought for 3s{anthropic_proxy.ANSI_RESET}\n回答"
        )
        messages = [{"role": "assistant", "content": text}]
        result = anthropic_proxy.strip_thinking_text_from_messages(messages)
        assert "Thought for" not in result[0]["content"]
        assert "回答" in result[0]["content"]

    def test_non_assistant_untouched(self):
        messages = [{"role": "user", "content": self._thinking_block("想")}]
        result = anthropic_proxy.strip_thinking_text_from_messages(messages)
        assert result[0]["content"] == messages[0]["content"]

    def test_empty_text_block_dropped(self):
        messages = [{"role": "assistant", "content": [
            {"type": "text", "text": self._thinking_block("想")},
        ]}]
        result = anthropic_proxy.strip_thinking_text_from_messages(messages)
        assert result[0]["content"] == []

    def test_non_list_passthrough(self):
        assert anthropic_proxy.strip_thinking_text_from_messages("oops") == "oops"


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
# T2.5 thinking 文本化：流式状态机
# ---------------------------------------------------------------------------

class TestStreamThinkingAsText:
    """thinking_as_text=True 时 reasoning 增量 → 灰色 text 块。"""

    @staticmethod
    def _run(converter, chunks):
        raw = bytearray()
        for chunk in chunks:
            raw += converter.feed(chunk)
        raw += converter.finish()
        return _parse_events(bytes(raw))

    @staticmethod
    def _block_text(events, index):
        return "".join(
            data["delta"]["text"] for name, data in events
            if name == "content_block_delta" and data["index"] == index
        )

    def _converter(self, clock=None):
        return anthropic_proxy.AnthropicStreamConverter(
            "claude-sonnet-4-5",
            thinking_as_text=True,
            time_source=clock or (lambda: 0.0),
        )

    def test_thinking_as_text_streams_complete_lines(self):
        converter = self._converter()
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "第一行\n第二"}}]},
            {"choices": [{"delta": {"reasoning_content": "行\n"}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ])
        starts = [data for name, data in events if name == "content_block_start"]
        assert [s["content_block"]["type"] for s in starts] == ["text", "text"]
        thinking = self._block_text(events, 0)
        assert thinking.startswith(anthropic_proxy.THINKING_TEXT_HEADER_LINE)
        assert f"\n{anthropic_proxy.ANSI_DIM}第一行{anthropic_proxy.ANSI_RESET}" in thinking
        assert f"\n{anthropic_proxy.ANSI_DIM}第二行{anthropic_proxy.ANSI_RESET}" in thinking
        assert "💭 Thought for 1s" in thinking
        assert thinking.endswith("\n")
        assert self._block_text(events, 1) == "答案"
        assert not any(
            data["delta"]["type"] == "thinking_delta"
            for name, data in events if name == "content_block_delta"
        )

    def test_thinking_as_text_buffers_incomplete_line(self):
        converter = self._converter()
        first = _parse_events(converter.feed(
            {"choices": [{"delta": {"reasoning_content": "第一行\n半行"}}]}
        ))
        text = "".join(
            data["delta"]["text"] for name, data in first
            if name == "content_block_delta"
        )
        assert "第一行" in text
        assert "半行" not in text

    def test_thinking_as_text_blank_segment_emits_nothing(self):
        converter = self._converter()
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "  \n \n"}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ])
        starts = [data for name, data in events if name == "content_block_start"]
        assert len(starts) == 1
        assert self._block_text(events, 0) == "答案"

    def test_thinking_as_text_then_tool_call_closes_block(self):
        converter = self._converter()
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "思考\n"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "echo", "arguments": "{}"}},
            ]}, "finish_reason": "tool_calls"}]},
        ])
        starts = [
            data["content_block"]["type"] for name, data in events
            if name == "content_block_start"
        ]
        assert starts == ["text", "tool_use"]
        stops = [data["index"] for name, data in events if name == "content_block_stop"]
        assert stops == [0, 1]
        assert "💭 Thought for 1s" in self._block_text(events, 0)

    def test_thinking_as_text_finish_flushes_tail(self):
        converter = self._converter()
        raw = converter.feed({"choices": [{"delta": {"reasoning_content": "尾行无换行"}}]})
        raw += converter.finish()
        events = _parse_events(raw)
        text = self._block_text(events, 0)
        assert f"\n{anthropic_proxy.ANSI_DIM}尾行无换行{anthropic_proxy.ANSI_RESET}" in text
        assert "💭 Thought for 1s" in text
        names = [name for name, _ in events]
        assert names.index("content_block_stop") < names.index("message_delta")

    def test_thinking_as_text_disabled_keeps_native_thinking(self):
        converter = anthropic_proxy.AnthropicStreamConverter("claude-sonnet-4-5")
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "原生"}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ])
        starts = [
            data["content_block"]["type"] for name, data in events
            if name == "content_block_start"
        ]
        assert starts[0] == "thinking"

    def test_thinking_as_text_strips_esc_sequences(self):
        converter = self._converter()
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "前\x1b[31m红\x1b[0m后\n"}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ])
        text = self._block_text(events, 0)
        assert "\x1b[31m" not in text
        assert "前红后" in text
        assert anthropic_proxy.ANSI_DIM in text

    def test_thinking_as_text_multiple_segments_have_own_durations(self):
        ticks = iter([0.0, 2.0, 10.0, 15.0])

        def clock():
            return next(ticks, 15.0)  # 耗尽后回落末值，避免实现变动触发 StopIteration

        converter = self._converter(clock=clock)
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "第一段\n"}}]},
            {"choices": [{"delta": {"content": "回答一"}}]},
            {"choices": [{"delta": {"reasoning_content": "第二段\n"}}]},
            {"choices": [{"delta": {"content": "回答二"}, "finish_reason": "stop"}]},
        ])
        starts = [
            data["content_block"]["type"] for name, data in events
            if name == "content_block_start"
        ]
        assert starts == ["text", "text", "text", "text"]
        assert "💭 Thought for 2s" in self._block_text(events, 0)
        assert "💭 Thought for 5s" in self._block_text(events, 2)

    def test_thinking_as_text_block_limit_discards_partial_segment(self):
        converter = self._converter()
        first = _parse_events(converter.feed(
            {"choices": [{"delta": {"reasoning_content": "开头\n"}}]}
        ))
        assert any(name == "content_block_start" for name, _ in first)
        limit = anthropic_proxy.MAX_THINKING_TEXT_BLOCK_BYTES
        second = _parse_events(converter.feed(
            {"choices": [{"delta": {"reasoning_content": "b" * limit}}]}
        ))
        assert any(name == "content_block_stop" for name, _ in second)
        assert not any(
            "Thought for" in data["delta"].get("text", "")
            for name, data in second if name == "content_block_delta"
        )
        assert not any(name == "content_block_delta" for name, _ in second)
        # 丢弃后正常回答仍应下发
        rest = _parse_events(converter.feed(
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]}
        ))
        assert any(
            data["delta"].get("text") == "答案"
            for name, data in rest if name == "content_block_delta"
        )

    def test_thinking_as_text_block_limit_discards_before_start(self):
        converter = self._converter()
        limit = anthropic_proxy.MAX_THINKING_TEXT_BLOCK_BYTES
        events = self._run(converter, [
            {"choices": [{"delta": {"reasoning_content": "b" * (limit + 1)}}]},
            {"choices": [{"delta": {"content": "答案"}, "finish_reason": "stop"}]},
        ])
        starts = [
            data["content_block"]["type"] for name, data in events
            if name == "content_block_start"
        ]
        assert starts == ["text"]
        assert self._block_text(events, 0) == "答案"

    def test_thinking_as_text_response_limit_discards_later_segments(self):
        converter = self._converter()
        segment = "a" * (200 * 1024)  # 单段低于 256 KiB
        chunks = []
        for _ in range(6):
            chunks.append({"choices": [{"delta": {"reasoning_content": segment}}]})
            chunks.append({"choices": [{"delta": {"content": "x"}}]})
        chunks.append({"choices": [{"delta": {"content": "结束"}, "finish_reason": "stop"}]})
        events = self._run(converter, chunks)
        texts = {}
        for name, data in events:
            if name == "content_block_delta" and data["delta"]["type"] == "text_delta":
                texts[data["index"]] = texts.get(data["index"], "") + data["delta"]["text"]
        thinking_blocks = [text for text in texts.values() if "💭 Thinking" in text]
        # 5 × 200 KiB = 1,024,000 < 1 MiB；第 6 段累计超限后被整体吞掉
        assert len(thinking_blocks) == 5


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

    def test_record_usage_writes_cache_creation(self, monkeypatch):
        """直调 _record_usage：cache 模拟的写入量写入日志字段。"""
        captured = {}
        monkeypatch.setattr(anthropic_proxy, "record_usage_fn",
                            lambda entry: captured.update(entry))
        anthropic_proxy._record_usage(
            {"model": "GLM-5.2"}, "GLM-5.2", False,
            {"prompt_tokens": 40, "completion_tokens": 5},
            (10, 20, 10), None, time.monotonic(), client_ip="127.0.0.1",
        )
        assert captured["cache_read_tokens"] == 10
        assert captured["cache_creation_tokens"] == 20
        assert captured["input_tokens"] == 10  # 40 - 10 - 20
        assert captured["output_tokens"] == 5

    def test_stream_success_records_usage(self, client, monkeypatch, tmp_path):
        """流式成功收尾 → record 被调用一次，含 client_ip 与 usage。"""
        from src.usage import usage_log
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
        from src.usage import usage_log

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
        from src.usage import usage_log
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
        from src.usage import usage_log
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
        from src.usage import usage_log

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


# ---------------------------------------------------------------------------
# 路由接线：UA 门控 × 开关 × 历史剥离（任务 4/5）
# ---------------------------------------------------------------------------

CC_UA = "claude-cli/1.0.0"


class TestThinkingAsTextRouting:
    """覆盖 UA × CODEARTS_THINKING_AS_TEXT（含缺失/别名/大小写）与历史剥离的端到端接线。"""

    @pytest.fixture(autouse=True)
    def _config(self, monkeypatch):
        monkeypatch.setattr(config_store, "_data", {
            "CODEARTS_CACHE_SIM": "0",
            "CODEARTS_THINKING_AS_TEXT": "1",
        })

    @staticmethod
    def _thinking_text() -> str:
        """上一轮已渲染的思考文本（头行 + 灰色行 + 时长行）。"""
        dim = anthropic_proxy.ANSI_DIM
        reset = anthropic_proxy.ANSI_RESET
        return (
            f"{anthropic_proxy.THINKING_TEXT_HEADER_LINE}\n"
            f"{dim}旧思考{reset}\n"
            f"{dim}💭 Thought for 3s{reset}\n"
        )

    @classmethod
    def _body(cls, stream: bool) -> dict:
        return {
            "model": "GLM-5.2",
            "max_tokens": 10,
            "stream": stream,
            "messages": [
                {"role": "user", "content": "第一轮"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": cls._thinking_text() + "第一轮回答"},
                ]},
                {"role": "user", "content": "第二轮"},
            ],
        }

    @staticmethod
    def _stream_lines() -> list:
        return [
            b'data: {"choices":[{"delta":{"reasoning_content":"\\u601d\\u8003\\n"}}]}',
            b'data: {"choices":[{"delta":{"content":"\\u7b54\\u6848"},"finish_reason":"stop"}]}',
            b"data: [DONE]",
        ]

    @staticmethod
    def _json_payload() -> dict:
        return {
            "choices": [{
                "message": {"role": "assistant", "content": "答案",
                            "reasoning_content": "思考"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 7, "completion_tokens": 1},
        }

    @pytest.mark.parametrize("ua,switch,expect_text", [
        (CC_UA, "1", True),
        (CC_UA, "0", False),
        (CC_UA, None, True),  # 配置缺失 → 默认开启
        ("curl/8.0", "1", False),
        ("curl/8.0", "0", False),
    ])
    def test_stream_ua_gate_matrix(self, client, monkeypatch, ua, switch, expect_text):
        """流式：仅 CC UA 且开关开启时思考改写为灰色文本。"""
        data = {"CODEARTS_CACHE_SIM": "0"}
        if switch is not None:
            data["CODEARTS_THINKING_AS_TEXT"] = switch
        monkeypatch.setattr(config_store, "_data", data)
        monkeypatch.setattr(
            anthropic_proxy.requests, "post",
            lambda *a, **kw: _FakeStreamResponse(self._stream_lines()),
        )
        resp = client.post("/v1/messages", json=self._body(True),
                           headers={"User-Agent": ua})
        assert resp.status_code == 200
        events = _parse_events(resp.get_data())
        starts = [
            data["content_block"]["type"] for name, data in events
            if name == "content_block_start"
        ]
        if expect_text:
            assert "thinking" not in starts
            assert starts.count("text") >= 1
            texts = "".join(
                data["delta"]["text"] for name, data in events
                if name == "content_block_delta"
                and data["delta"]["type"] == "text_delta"
            )
            assert "💭 Thinking" in texts
            assert "思考" in texts
            assert "💭 Thought for" in texts
            assert "答案" in texts
        else:
            assert "thinking" in starts
            assert any(
                data["delta"].get("type") == "thinking_delta"
                for name, data in events if name == "content_block_delta"
            )
            assert not any("💭 Thinking" in data["delta"].get("text", "")
                           for name, data in events
                           if name == "content_block_delta")

    @pytest.mark.parametrize("ua,switch,expect_text", [
        (CC_UA, "1", True),
        (CC_UA, "0", False),
        (CC_UA, "false", False),  # 关闭值大小写/别名兼容
        (CC_UA, None, True),  # 配置缺失 → 默认开启
        ("curl/8.0", "1", False),
        ("curl/8.0", "0", False),
    ])
    def test_non_stream_ua_gate_matrix(self, client, monkeypatch, ua, switch, expect_text):
        """非流式：仅 CC UA 且开关开启时思考改写为独立 text 块。"""
        data = {"CODEARTS_CACHE_SIM": "0"}
        if switch is not None:
            data["CODEARTS_THINKING_AS_TEXT"] = switch
        monkeypatch.setattr(config_store, "_data", data)
        monkeypatch.setattr(
            anthropic_proxy.requests, "post",
            lambda *a, **kw: _FakeJsonResponse(self._json_payload()),
        )
        resp = client.post("/v1/messages", json=self._body(False),
                           headers={"User-Agent": ua})
        assert resp.status_code == 200
        blocks = resp.get_json()["content"]
        types = [block["type"] for block in blocks]
        if expect_text:
            assert types == ["text", "text"]
            assert "💭 Thinking" in blocks[0]["text"]
            assert "思考" in blocks[0]["text"]
            assert "Thought for" not in blocks[0]["text"]
            assert blocks[1]["text"] == "答案"
        else:
            assert types[0] == "thinking"
            assert blocks[0]["thinking"] == "思考"

    def _capture_upstream(self, monkeypatch, response):
        captured = {}

        def fake_post(url, data=None, headers=None, **kwargs):
            captured["payload"] = json.loads(data.decode("utf-8"))
            return response

        monkeypatch.setattr(anthropic_proxy.requests, "post", fake_post)
        return captured

    def test_cc_ua_history_strip_upstream_payload(self, client, monkeypatch):
        """CC UA：转发上游的 assistant 历史不含思考文本，回答保留。"""
        captured = self._capture_upstream(
            monkeypatch, _FakeJsonResponse(self._json_payload())
        )
        resp = client.post("/v1/messages", json=self._body(False),
                           headers={"User-Agent": CC_UA})
        assert resp.status_code == 200
        payload_text = json.dumps(captured["payload"], ensure_ascii=False)
        assert "💭 Thinking" not in payload_text
        assert "旧思考" not in payload_text
        assert "Thought for" not in payload_text
        assert "第一轮回答" in payload_text

    def test_switch_off_still_history_strip(self, client, monkeypatch):
        """开关关闭：思考块保持原生，但历史剥离仍生效（与 cc-proxy 一致）。"""
        monkeypatch.setattr(config_store, "_data", {
            "CODEARTS_CACHE_SIM": "0",
            "CODEARTS_THINKING_AS_TEXT": "0",
        })
        captured = self._capture_upstream(
            monkeypatch, _FakeJsonResponse(self._json_payload())
        )
        resp = client.post("/v1/messages", json=self._body(False),
                           headers={"User-Agent": CC_UA})
        assert resp.status_code == 200
        payload_text = json.dumps(captured["payload"], ensure_ascii=False)
        assert "💭 Thinking" not in payload_text
        assert "第一轮回答" in payload_text
        assert resp.get_json()["content"][0]["type"] == "thinking"

    def test_non_cc_ua_no_history_strip(self, client, monkeypatch):
        """非 CC UA：历史思考文本原样转发（不做剥离）。"""
        captured = self._capture_upstream(
            monkeypatch, _FakeJsonResponse(self._json_payload())
        )
        resp = client.post("/v1/messages", json=self._body(False),
                           headers={"User-Agent": "curl/8.0"})
        assert resp.status_code == 200
        payload_text = json.dumps(captured["payload"], ensure_ascii=False)
        assert "💭 Thinking" in payload_text
        assert "旧思考" in payload_text

    def test_cache_estimate_uses_history_strip_body(self, client, monkeypatch):
        """缓存估算输入为剥离后的消息，而非原始 body。"""
        captured = {}
        real_estimate = anthropic_proxy.cache_sim.estimate_body_tokens

        def capture_estimate(body):
            captured["body"] = body
            return real_estimate(body)

        monkeypatch.setattr(
            anthropic_proxy.cache_sim, "estimate_body_tokens", capture_estimate
        )
        monkeypatch.setattr(
            anthropic_proxy.requests, "post",
            lambda *a, **kw: _FakeStreamResponse(self._stream_lines()),
        )
        resp = client.post("/v1/messages", json=self._body(True),
                           headers={"User-Agent": CC_UA})
        resp.get_data()  # 完整消费流式响应
        assert "body" in captured
        estimate_text = json.dumps(captured["body"], ensure_ascii=False)
        assert "💭 Thinking" not in estimate_text
        assert "第一轮回答" in estimate_text


# ---------------------------------------------------------------------------
# 未登录降级（design D10）：signed_headers_fn 抛 CredentialsUnavailable → 503
# ---------------------------------------------------------------------------

class TestNotLoggedInDegradation:
    def _body(self):
        return {
            "model": "claude-sonnet-4-5",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
        }

    def test_messages_returns_503_envelope_when_not_logged_in(
            self, client, monkeypatch):
        monkeypatch.setattr(
            anthropic_proxy, "signed_headers_fn",
            lambda body, benefit, model_id: (_ for _ in ()).throw(
                anthropic_proxy.oauth_login.CredentialsUnavailable(
                    "oauth not logged in")),
        )
        resp = client.post("/v1/messages", json=self._body())
        assert resp.status_code == 503
        payload = resp.get_json()
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "api_error"
        assert payload["error"]["message"] == "尚未登录 CodeArts，请先在管理页完成登录"

    def test_messages_upstream_error_still_502_when_logged_in(
            self, client, monkeypatch):
        """正常 502 路径不受 503 分支影响（退避清零防慢测）。"""
        monkeypatch.setattr(anthropic_proxy, "_RETRY_DELAYS", ())
        monkeypatch.setattr(
            anthropic_proxy, "signed_headers_fn",
            lambda body, benefit, model_id: {"x-test": "1"},
        )
        monkeypatch.setattr(
            anthropic_proxy.requests, "post",
            lambda *a, **kw: (_ for _ in ()).throw(
                anthropic_proxy.requests.ConnectionError("boom")),
        )
        resp = client.post("/v1/messages", json=self._body())
        assert resp.status_code == 502
