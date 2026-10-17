# -*- coding: utf-8 -*-
"""usage_log 单元测试（临时目录隔离，不触碰真实 usage-log.jsonl）。"""
import json

import pytest

import usage_log


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """重置模块状态并在临时目录初始化。"""
    monkeypatch.setattr(usage_log, "_initialized", False)
    usage_log.init(str(tmp_path))
    yield tmp_path
    monkeypatch.setattr(usage_log, "_initialized", False)


def _entry(model="glm-5.3-flash", **overrides):
    entry = {
        "ts": "2026-10-17T10:00:00",
        "endpoint": "anthropic",
        "ip": "127.0.0.1",
        "model": model,
        "stream": True,
        "effort": None,
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 50,
        "first_token_ms": 300,
        "duration_ms": 1500,
    }
    entry.update(overrides)
    return entry


def test_record_and_query(isolated):
    usage_log.record(_entry())
    usage_log.record(_entry(model="openpangu-2.0-pro", input_tokens=10, output_tokens=5, cache_read_tokens=0))
    result = usage_log.query()
    assert result["summary"]["requests"] == 2
    assert result["summary"]["input_tokens"] == 110
    assert result["summary"]["output_tokens"] == 25
    assert result["summary"]["cache_read_tokens"] == 50
    assert result["by_model"]["glm-5.3-flash"]["requests"] == 1
    assert result["by_model"]["openpangu-2.0-pro"]["input_tokens"] == 10
    assert result["total"] == 2
    assert result["entries"][0]["model"] == "openpangu-2.0-pro"  # 倒序，最新在前


def test_query_pagination(isolated):
    for index in range(7):
        usage_log.record(_entry(ts=f"t{index}"))
    result = usage_log.query(page=1, limit=3)
    assert result["total"] == 7
    assert [e["ts"] for e in result["entries"]] == ["t6", "t5", "t4"]
    page2 = usage_log.query(page=2, limit=3)
    assert [e["ts"] for e in page2["entries"]] == ["t3", "t2", "t1"]


def test_replay_rebuilds_summary(isolated):
    usage_log.record(_entry())
    usage_log.record(_entry(model="GLM-5.2", input_tokens=7))
    usage_log.init(str(isolated))  # 重新回放
    result = usage_log.query()
    assert result["summary"]["requests"] == 2
    assert result["summary"]["input_tokens"] == 107
    assert result["total"] == 2


def test_record_before_init_is_noop(tmp_path):
    monkey_unused = None
    usage_log.record(_entry())  # 未 init 时静默忽略
    assert not (tmp_path / "usage-log.jsonl").exists()


def test_rolls_back_file_when_over_threshold(isolated, monkeypatch):
    monkeypatch.setattr(usage_log, "_MAX_FILE_ENTRIES", 10)
    for index in range(12):
        usage_log.record(_entry(ts=f"t{index}"))
    usage_log.init(str(isolated))  # 触发滚动
    lines = (isolated / "usage-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5  # 保留较新一半
    assert json.loads(lines[0])["ts"] == "t7"


def test_corrupt_line_skipped(isolated):
    path = isolated / "usage-log.jsonl"
    good = json.dumps(_entry(), ensure_ascii=False)
    path.write_text(good + "\n" + 'garbage line\n' + good + "\n", encoding="utf-8")
    usage_log.init(str(isolated))
    result = usage_log.query()
    assert result["total"] == 2


def test_not_initialized_query_returns_empty():
    import usage_log as module
    saved = module._initialized
    module._initialized = False
    try:
        result = module.query()
        assert result["entries"] == []
        assert result["total"] == 0
    finally:
        module._initialized = saved
