# -*- coding: utf-8 -*-
"""OpenAI 端点使用日志 reasoning_tokens 采集测试（mock 上游，三路径覆盖）。

覆盖 spec「思考 token 用量采集」的 OpenAI 侧路径：流式上游细分字段、
流式 delta 估算兜底、非流式聚合结果、上游误回 JSON 伪装成流式。
测试骨架沿用 test_api_key_gate.py 的 server 安全导入模式，并以
_FakeStreamResponse / _FakeJsonResponse 注入假上游响应。
"""
import json

import pytest

from src.auth import api_keys
from src.config import config_store
from src.usage import usage_log


@pytest.fixture(scope="module")
def server_module():
    """安全 import server（先注入假配置，避免真实文件读写与上游请求）。"""
    mp = pytest.MonkeyPatch()
    mp.setattr(config_store, "init", lambda path: None)
    mp.setattr(config_store, "get", lambda key, default="": {
        "CODEARTS_PROXY_PORT": "51818",
        "ADMIN_PASSWORD": "test-pw",
        "ADMIN_SECRET_KEY": "test-secret",
    }.get(key, default))
    mp.setattr(api_keys, "init", lambda path: None)
    try:
        from src import server as mod
    finally:
        mp.undo()
    mod.app.testing = True
    return mod


class _NoopLease:
    """无操作并发租约（测试不关心并发槽位）。"""

    counted = False

    def attach(self, response):
        return response

    def release(self):
        pass


class _FakeStreamResponse:
    """假流式上游响应（iter_lines 产出 SSE 行）。"""

    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200
        self.headers = {"Content-Type": "text/event-stream"}

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line


class _FakeJsonResponse:
    """假非流式上游响应（Content-Type 非 SSE）。"""

    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.text = json.dumps(payload)

    @property
    def content(self):
        return self.text.encode("utf-8")


@pytest.fixture
def usage_capture(monkeypatch, tmp_path):
    """隔离 usage_log 到临时目录，yield 其 query()。"""
    monkeypatch.setattr(usage_log, "_initialized", False)
    usage_log.init(str(tmp_path))
    yield usage_log
    monkeypatch.setattr(usage_log, "_initialized", False)


@pytest.fixture
def client(server_module, monkeypatch, tmp_path, usage_capture):
    """隔离 API Key 与模型列表，屏蔽刷新与真实 OAuth。"""
    api_keys.init(str(tmp_path / "api-keys.json"))
    monkeypatch.setattr(server_module, "ADMIN_PASSWORD", "")
    monkeypatch.setattr(server_module, "_refresh_models_if_needed", lambda: None)
    monkeypatch.setattr(server_module, "SUPPORTED_MODELS", ["glm-5.3-flash"])
    monkeypatch.setattr(server_module, "MODEL_DETAILS", {})
    monkeypatch.setattr(server_module, "AGENT_MODELS", [])
    # 签名头 stub：隔离 OAuth 凭据全局状态（其他测试文件可能留下未登录态，
    # 真实 _hmac_headers 会抛 CredentialsUnavailable 使请求提前 503）
    monkeypatch.setattr(
        server_module, "_hmac_headers",
        lambda body, benefit=False, model_id="": {"x-test": "1"},
    )
    monkeypatch.setattr(server_module, "_acquire_upstream_lease", lambda timeout=None: _NoopLease())
    return server_module.app.test_client()


def _sse(chunks) -> list:
    """构造合法 SSE 行列表；字符串项视为 [DONE] 等裸标记。"""
    lines = []
    for chunk in chunks:
        if isinstance(chunk, str):
            lines.append(f"data: {chunk}".encode("utf-8"))
        else:
            lines.append(b"data: " + json.dumps(chunk).encode("utf-8"))
    return lines


def _post(client, server_module, monkeypatch, response, stream=True):
    """注入假上游并 POST 一次聊天补全，返回响应。"""
    monkeypatch.setattr(server_module.requests, "post", lambda *a, **kw: response)
    body = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]}
    if stream:
        body["stream"] = True
    return client.post("/v1/chat/completions", json=body)


def _entry(usage_capture):
    return usage_capture.query()["entries"][0]


# ---------------------------------------------------------------------------
# 流式路径
# ---------------------------------------------------------------------------

def test_stream_prefers_upstream_reasoning_detail(client, server_module, monkeypatch, usage_capture):
    """上游 usage 含 reasoning_tokens 细分 → 精确值入库（优先于估算）。"""
    lines = _sse([
        {"choices": [{"delta": {"reasoning_content": "x" * 400}}]},
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 100,
                   "completion_tokens_details": {"reasoning_tokens": 42}}},
        "[DONE]",
    ])
    resp = _post(client, server_module, monkeypatch, _FakeStreamResponse(lines))
    assert resp.status_code == 200
    resp.get_data()  # 消费流，触发 generator 收尾的 record
    entry = _entry(usage_capture)
    assert entry["reasoning_tokens"] == 42
    assert entry["output_tokens"] == 100  # 细分优先：400 字符估算(100)未取代 42


def test_stream_estimates_from_reasoning_delta(client, server_module, monkeypatch, usage_capture):
    """无细分字段但有 reasoning_content 增量 → 按字符数 ÷ 4 估算（不持全文）。"""
    lines = _sse([
        {"choices": [{"delta": {"reasoning_content": "y" * 800}}]},
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 500}},
        "[DONE]",
    ])
    resp = _post(client, server_module, monkeypatch, _FakeStreamResponse(lines))
    resp.get_data()
    assert _entry(usage_capture)["reasoning_tokens"] == 200


def test_stream_without_reasoning_omits_key(client, server_module, monkeypatch, usage_capture):
    """无细分字段且无思考文本 → 条目省略 reasoning_tokens 键。"""
    lines = _sse([
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        "[DONE]",
    ])
    resp = _post(client, server_module, monkeypatch, _FakeStreamResponse(lines))
    resp.get_data()
    assert "reasoning_tokens" not in _entry(usage_capture)


def test_stream_json_fallback_path(client, server_module, monkeypatch, usage_capture):
    """上游误回普通 JSON（非 SSE）→ 包装为单帧流，同样采集思考量。"""
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "ok",
                                 "reasoning_content": "z" * 400},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 300},
    }

    class _JsonAsStream(_FakeJsonResponse):
        headers = {"Content-Type": "application/json"}

    resp = _post(client, server_module, monkeypatch, _JsonAsStream(payload))
    resp.get_data()
    assert _entry(usage_capture)["reasoning_tokens"] == 100


# ---------------------------------------------------------------------------
# 非流式路径
# ---------------------------------------------------------------------------

def test_non_stream_records_upstream_detail(client, server_module, monkeypatch, usage_capture):
    """非流式 + 上游细分字段 → 精确值入库。"""
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 200,
                  "completion_tokens_details": {"reasoning_tokens": 77}},
    }
    resp = _post(client, server_module, monkeypatch, _FakeJsonResponse(payload), stream=False)
    assert resp.status_code == 200
    assert _entry(usage_capture)["reasoning_tokens"] == 77


def test_non_stream_estimates_from_message(client, server_module, monkeypatch, usage_capture):
    """非流式 + 无细分字段 + message.reasoning_content → 字符数 ÷ 4 估算。"""
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "ok",
                                 "reasoning_content": "r" * 1200},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 800},
    }
    resp = _post(client, server_module, monkeypatch, _FakeJsonResponse(payload), stream=False)
    assert resp.status_code == 200
    assert _entry(usage_capture)["reasoning_tokens"] == 300


def test_non_stream_clamps_to_output_tokens(client, server_module, monkeypatch, usage_capture):
    """估算值超过 output_tokens 时截断（思考为输出的子集）。"""
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "ok",
                                 "reasoning_content": "q" * 4000},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 100},
    }
    resp = _post(client, server_module, monkeypatch, _FakeJsonResponse(payload), stream=False)
    assert resp.status_code == 200
    assert _entry(usage_capture)["reasoning_tokens"] == 100


def test_non_stream_dirty_reasoning_value_falls_back(client, server_module, monkeypatch, usage_capture):
    """上游细分字段非法（负数）→ 退化为估算，不写脏值。"""
    payload = {
        "choices": [{"message": {"role": "assistant", "content": "ok",
                                 "reasoning_content": "s" * 120},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 300,
                  "completion_tokens_details": {"reasoning_tokens": -9}},
    }
    resp = _post(client, server_module, monkeypatch, _FakeJsonResponse(payload), stream=False)
    assert resp.status_code == 200
    assert _entry(usage_capture)["reasoning_tokens"] == 30


# ---------------------------------------------------------------------------
# 脏结构防御（helper 不得抛未捕获异常）
# ---------------------------------------------------------------------------

def test_non_stream_dirty_message_still_200(client, server_module, monkeypatch, usage_capture):
    """上游 message 为非 dict 的脏结构 → 仍返回 200（helper 不得抛未捕获异常）。"""
    payload = {"choices": [{"message": "oops", "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
    resp = _post(client, server_module, monkeypatch, _FakeJsonResponse(payload), stream=False)
    assert resp.status_code == 200
    assert "reasoning_tokens" not in _entry(usage_capture)


def test_stream_json_fallback_dirty_message_still_200(client, server_module, monkeypatch, usage_capture):
    """流式 JSON 兜底分支同类脏结构 → 仍返回 200 单帧流。"""
    payload = {"choices": [{"message": ["bad"], "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 20}}
    resp = _post(client, server_module, monkeypatch, _FakeJsonResponse(payload))
    assert resp.status_code == 200
    resp.get_data()
    assert "reasoning_tokens" not in _entry(usage_capture)