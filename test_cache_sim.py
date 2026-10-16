# -*- coding: utf-8 -*-
"""cache_sim 单元测试：估算口径 / 指纹命中 / TTL / 封顶 / 不变性 / 注入字段。"""
import time
import types

import pytest

import anthropic_proxy
import cache_sim
from cache_sim import (
    FingerprintTracker,
    build_profile,
    estimate_tokens,
    select_cache_usage,
)


@pytest.fixture(autouse=True)
def _env_enabled(monkeypatch):
    monkeypatch.setenv("CODEARTS_CACHE_SIM", "1")


@pytest.fixture
def tracker():
    return FingerprintTracker()


def _body(texts, system="sys prompt", tools=None):
    messages = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": t}
        for i, t in enumerate(texts)
    ]
    body = {"model": "m", "max_tokens": 100, "messages": messages}
    if system is not None:
        body["system"] = system
    if tools:
        body["tools"] = tools
    return body


# -- 估算口径 ----------------------------------------------------------------

def test_estimate_tokens():
    assert estimate_tokens("") == 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens("abcd") == 1  # 4//4
    assert estimate_tokens("ab") == 1  # 短串保护：max(1, ...)
    assert estimate_tokens("a" * 401) == 100


def test_profile_segments_and_monotonic():
    body = _body(["hello world", "hi again"])
    profile = build_profile(body)
    # system + 2 messages → 3 段；无 tools 无 T 段
    assert len(profile) == 3
    # 累积哈希：相同前缀的请求，前缀段哈希逐段一致
    body2 = _body(["hello world", "hi again", "new turn"])
    profile2 = build_profile(body2)
    assert [p["hash"] for p in profile] == [p["hash"] for p in profile2[:3]]
    # cumulative 单调递增
    tokens = [p["cumulative_tokens"] for p in profile2]
    assert tokens == sorted(tokens) and tokens[-1] > 0


def test_tools_change_invalidates_message_hashes():
    profile_a = build_profile(_body(["a"], tools=[{"name": "t1", "input_schema": {}}]))
    profile_b = build_profile(_body(["a"], tools=[{"name": "t2", "input_schema": {}}]))
    # 段序：0=system、1=tools、2=message
    assert profile_a[1]["hash"] != profile_b[1]["hash"]  # T 段不同
    assert profile_a[2]["hash"] != profile_b[2]["hash"]  # 其后 M 段哈希全部失效


# -- 指纹追踪 ----------------------------------------------------------------

def test_first_request_no_hit(tracker):
    profile = build_profile(_body(["a", "b"]))
    assert tracker.compute("default", profile, 1000) is None


def test_second_request_hits(tracker):
    profile = build_profile(_body(["a", "b"]))
    tracker.update("default", profile)
    read, creation, plain = tracker.compute("default", profile, 1000)
    # 指纹层无法区分新后缀与最后一条消息，命中后余量全部归 creation
    assert read > 0 and plain == 0 and read + creation == 1000
    assert read <= 1000 * cache_sim.CACHE_READ_CAP_RATIO


def test_partial_prefix_hit(tracker):
    tracker.update("default", build_profile(_body(["a", "b"])))
    # 追加新消息：前缀命中，余量计入 creation，不变式成立
    read, creation, plain = tracker.compute(
        "default", build_profile(_body(["a", "b", "c"])), 1000
    )
    assert read > 0
    assert read + creation + plain == 1000


def test_divergence_no_hit(tracker):
    tracker.update("default", build_profile(_body(["a", "b"])))
    # system 也不同 → 首段即失配 → None（走前缀兜底）
    assert tracker.compute(
        "default", build_profile(_body(["a"], system="other")), 1000
    ) is None


def test_system_prefix_survives_message_divergence(tracker):
    tracker.update("default", build_profile(_body(["a", "b"])))
    # 消息全变但 system 相同 → 仅 S 段命中
    read, creation, plain = tracker.compute(
        "default", build_profile(_body(["x", "y"])), 1000
    )
    assert read == build_profile(_body(["x"]))[0]["cumulative_tokens"]
    assert read + creation + plain == 1000


def test_ttl_expiry(tracker, monkeypatch):
    profile = build_profile(_body(["a", "b"]))
    tracker.update("default", profile)
    fake_time = types.SimpleNamespace(monotonic=lambda: time.monotonic() + cache_sim.TTL_5M + 1)
    monkeypatch.setattr(cache_sim, "time", fake_time)
    # 5m 过期 → 前缀截断 → 零命中，且表被清空（compute 有破坏性）
    assert tracker.compute("default", profile, 1000) is None
    assert tracker._tables["default"] == []


def test_cache_read_cap_85pct(tracker):
    profile = build_profile(_body(["a", "b"]))
    tracker.update("default", profile)
    matched = profile[-1]["cumulative_tokens"]
    total = max(1, matched // 2)  # 超小 total 触发 85% 封顶
    read, creation, plain = tracker.compute("default", profile, total)
    assert read == int(total * cache_sim.CACHE_READ_CAP_RATIO)
    assert read + creation + plain == total


def test_update_truncates_on_shrink(tracker):
    tracker.update("default", build_profile(_body(["a", "b", "c"])))
    tracker.update("default", build_profile(_body(["a", "z"])))
    table = tracker._tables["default"]
    # 失配尾部截断后追加新请求尾部段：S + M:user:a + M:assistant:z = 3 段
    assert len(table) == 3
    assert tracker.compute("default", build_profile(_body(["a"])), 100) is not None


# -- 主入口与不变性 ----------------------------------------------------------

def test_select_cache_usage_invariant(monkeypatch):
    monkeypatch.setattr(cache_sim, "tracker", FingerprintTracker())
    body = _body(["a", "b"])
    total = estimate_tokens("x" * 100)
    read, creation, plain = select_cache_usage(total, body)
    assert read + creation + plain == total
    # 首请求走前缀兜底：read=0，creation=前缀估算（含 system）
    assert read == 0 and creation > 0


def test_select_cache_usage_second_call_hits(monkeypatch):
    monkeypatch.setattr(cache_sim, "tracker", FingerprintTracker())
    body = _body(["a", "b"])
    total = 5000
    select_cache_usage(total, body)  # 首次：写入指纹表
    read, creation, plain = select_cache_usage(total, body)  # 第二次：命中
    assert read > 0 and read <= total * cache_sim.CACHE_READ_CAP_RATIO
    assert read + creation + plain == total


def test_select_cache_usage_commit_false_no_write(monkeypatch):
    """commit=False 只查询不回写：连续两次都走前缀兜底（指纹表始终为空）。"""
    fresh = FingerprintTracker()
    monkeypatch.setattr(cache_sim, "tracker", fresh)
    body = _body(["a", "b"])
    first = select_cache_usage(5000, body, commit=False)
    second = select_cache_usage(5000, body, commit=False)
    assert first[0] == 0 and second[0] == 0  # 两次均未命中
    assert fresh._tables.get("default") is None  # 指纹表未被写入


def test_select_cache_usage_disabled(monkeypatch):
    monkeypatch.setenv("CODEARTS_CACHE_SIM", "0")
    assert select_cache_usage(100, _body(["a"])) is None


def test_select_cache_usage_zero_total(monkeypatch):
    monkeypatch.setattr(cache_sim, "tracker", FingerprintTracker())
    assert select_cache_usage(0, _body(["a"])) is None


def test_anthropic_stream_converter_injects_cache_fields():
    from anthropic_proxy import AnthropicStreamConverter

    converter = AnthropicStreamConverter(
        "claude-test", cache_usage=(700, 100, 200)
    )
    events = (converter.feed({"choices": [{"index": 0, "delta": {"content": "hi"}}]})
              + converter.finish()).decode("utf-8")
    assert '"cache_read_input_tokens":700' in events
    assert '"cache_creation_input_tokens":100' in events
    # message_start 与 message_delta 同源：cache 字段两处一致
    assert events.count('"cache_read_input_tokens":700') == 2
    # S13 回归：message_start 的 input_tokens 用非缓存部分兜底（usage 未到达时）
    assert '"input_tokens":200' in events


def test_stream_converter_without_cache_usage_has_no_cache_fields():
    from anthropic_proxy import AnthropicStreamConverter

    converter = AnthropicStreamConverter("claude-test")
    events = (converter.feed({"choices": [{"index": 0, "delta": {"content": "hi"}}]})
              + converter.finish()).decode("utf-8")
    assert "cache_read_input_tokens" not in events


# -- 非流式注入链（CR M3）------------------------------------------------------

class TestNonStreamCacheInjection:
    """非流式 _usage_to_anthropic / openai_completion_to_anthropic 的注入与钳制。"""

    def _result(self, prompt_tokens=100):
        return {
            "id": "chatcmpl-1", "model": "openpangu-2.0-pro",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 5},
        }

    def test_fingerprint_hit_input_clamped_to_zero(self):
        """指纹命中：read+creation == prompt_tokens → input_tokens 钳为 0。"""
        message = anthropic_proxy.openai_completion_to_anthropic(
            self._result(100), "claude-test", cache_usage=(60, 40, 0)
        )
        usage = message["usage"]
        assert usage["input_tokens"] == 0
        assert usage["cache_read_input_tokens"] == 60
        assert usage["cache_creation_input_tokens"] == 40
        assert usage["output_tokens"] == 5

    def test_prefix_fallback_partial_creation(self):
        """前缀兜底：creation < prompt_tokens → input_tokens = 差值。"""
        message = anthropic_proxy.openai_completion_to_anthropic(
            self._result(100), "claude-test", cache_usage=(0, 30, 70)
        )
        usage = message["usage"]
        assert usage["input_tokens"] == 70
        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 30

    def test_no_cache_usage_no_cache_fields(self):
        """cache_usage=None：响应不含缓存字段（开关关闭路径）。"""
        message = anthropic_proxy.openai_completion_to_anthropic(self._result(100), "claude-test")
        assert "cache_read_input_tokens" not in message["usage"]
        assert "cache_creation_input_tokens" not in message["usage"]
        assert message["usage"]["input_tokens"] == 100

    def test_invariant_with_real_select_output(self, monkeypatch):
        """端到端不变式：select_cache_usage 产出经注入层后总和 == prompt_tokens。"""
        fresh = FingerprintTracker()
        monkeypatch.setattr(cache_sim, "tracker", fresh)
        body = _body(["a", "b"])
        cache_usage = cache_sim.select_cache_usage(100, body, commit=False)
        message = anthropic_proxy.openai_completion_to_anthropic(
            self._result(100), "claude-test", cache_usage=cache_usage
        )
        usage = message["usage"]
        assert sum(usage[k] for k in
                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) == 100

    def test_degenerate_values_clamp_input_non_negative(self):
        """越界 cache_usage：input_tokens 钳为 0 不出负数（真实链路不会产生此输入）。"""
        message = anthropic_proxy.openai_completion_to_anthropic(
            self._result(100), "claude-test", cache_usage=(200, 200, 0)
        )
        assert message["usage"]["input_tokens"] == 0
