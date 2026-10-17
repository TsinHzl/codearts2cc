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
        usage_log.record(_entry(ts=f"2026-10-17T10:00:{index:02d}"))
    # 第 11 条触发运行期滚动：保留较新一半（5 条），第 12 条再追加
    lines = (isolated / "usage-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6
    assert json.loads(lines[0])["ts"] == "2026-10-17T10:00:06"
    assert usage_log.query()["total"] == 6


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


def test_more_than_200_entries_still_pageable(isolated):
    for index in range(250):
        usage_log.record(_entry(ts=f"2026-10-17T10:{index // 60:02d}:{index % 60:02d}"))
    first = usage_log.query(page=1, limit=100)
    assert first["total"] == 250
    assert len(first["entries"]) == 100
    assert first["entries"][0]["ts"] == "2026-10-17T10:04:09"  # 第 250 条
    last = usage_log.query(page=3, limit=100)
    assert len(last["entries"]) == 50
    assert last["entries"][-1]["ts"] == "2026-10-17T10:00:00"


def test_date_range_filter_includes_both_ends(isolated):
    for day in (10, 11, 12, 13, 14):
        usage_log.record(_entry(ts=f"2026-10-{day:02d}T09:00:00"))
    usage_log.record({"ts": None, "model": "glm-5.3-flash"})  # 缺失 ts 不匹配任何区间
    result = usage_log.query(start="2026-10-11", end="2026-10-13")
    assert result["total"] == 3
    assert [e["ts"][:10] for e in result["entries"]] == [
        "2026-10-13", "2026-10-12", "2026-10-11",
    ]
    assert result["summary"]["requests"] == 3
    assert result["by_model"]["glm-5.3-flash"]["requests"] == 3
    assert usage_log.query(start="2026-10-13")["total"] == 2  # 仅起始日期


def test_daily_aggregation_ascending(isolated):
    usage_log.record(_entry(ts="2026-10-12T10:00:00", input_tokens=10, output_tokens=1,
                            cache_read_tokens=2, cache_creation_tokens=3))
    usage_log.record(_entry(ts="2026-10-11T10:00:00", input_tokens=20, output_tokens=2,
                            cache_read_tokens=4, cache_creation_tokens=5))
    usage_log.record(_entry(ts="2026-10-12T11:00:00", input_tokens=30, output_tokens=3,
                            cache_read_tokens=6, cache_creation_tokens=7))
    result = usage_log.query(daily="TRUE")
    assert [item["date"] for item in result["daily"]] == ["2026-10-11", "2026-10-12"]
    assert result["daily"][1] == {
        "date": "2026-10-12", "requests": 2, "input_tokens": 40, "output_tokens": 4,
        "cache_read_tokens": 8, "cache_creation_tokens": 10,
    }
    assert usage_log.query(daily="1")["daily"] is not None
    assert usage_log.query(daily="false")["daily"] is None
    assert usage_log.query()["daily"] is None
    filtered = usage_log.query(start="2026-10-12", daily=True)
    assert len(filtered["daily"]) == 1  # 聚合基于过滤后全量


def test_daily_by_model_groups_by_date_and_model(isolated):
    usage_log.record(_entry(model="b-model", ts="2026-10-12T10:00:00", input_tokens=10))
    usage_log.record(_entry(model="a-model", ts="2026-10-12T11:00:00", input_tokens=20))
    usage_log.record(_entry(model="a-model", ts="2026-10-11T09:00:00", input_tokens=5))
    result = usage_log.query(daily=True)
    assert [(row["date"], row["model"]) for row in result["daily_models"]] == [
        ("2026-10-11", "a-model"),
        ("2026-10-12", "a-model"),
        ("2026-10-12", "b-model"),
    ]
    assert result["daily_models"][1]["requests"] == 1
    assert result["daily_models"][1]["input_tokens"] == 20
    assert usage_log.query()["daily_models"] is None


def test_daily_by_model_normalizes_dirty_model(isolated):
    """脏 model（int / 不可哈希）归一为 unknown，daily 查询不抛错。"""
    usage_log.record(_entry(model=1, ts="2026-10-17T10:00:00"))
    usage_log.record(_entry(ts="2026-10-17T11:00:00"))
    result = usage_log.query(daily=True)
    assert [row["model"] for row in result["daily_models"]] == [
        "glm-5.3-flash", "unknown",
    ]
    rows = usage_log._daily_by_model([_entry(model=[], ts="2026-10-17T10:00:00")])
    assert rows[0]["model"] == "unknown"


def test_runtime_rollback_rebuilds_aggregates(isolated, monkeypatch):
    monkeypatch.setattr(usage_log, "_MAX_FILE_ENTRIES", 10)
    for index in range(11):
        usage_log.record(_entry(model="glm-5.3-flash" if index % 2 else "GLM-5.2"))
    result = usage_log.query()
    assert result["total"] == 5
    assert result["summary"]["requests"] == 5
    assert sum(bucket["requests"] for bucket in result["by_model"].values()) == 5
    assert usage_log._file_lines == 5
    lines = (isolated / "usage-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5


def test_query_returns_entry_copies(isolated):
    usage_log.record(_entry())
    first = usage_log.query()
    first["entries"][0]["model"] = "mutated"
    first["summary"]["requests"] = 999
    first["by_model"]["glm-5.3-flash"]["requests"] = 999
    second = usage_log.query()
    assert second["entries"][0]["model"] == "glm-5.3-flash"
    assert second["summary"]["requests"] == 1
    assert second["by_model"]["glm-5.3-flash"]["requests"] == 1


def test_init_rolls_back_file_when_over_threshold(isolated, monkeypatch):
    path = isolated / "usage-log.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for index in range(12):
            f.write(json.dumps(_entry(ts=f"2026-10-17T10:00:{index:02d}")) + "\n")
    monkeypatch.setattr(usage_log, "_MAX_FILE_ENTRIES", 10)
    usage_log.init(str(isolated))
    result = usage_log.query()
    assert result["total"] == 5
    assert len(path.read_text(encoding="utf-8").splitlines()) == 5


def test_rewrite_failure_backs_off(isolated, monkeypatch):
    monkeypatch.setattr(usage_log, "_MAX_FILE_ENTRIES", 10)
    calls = {"count": 0}

    def failing_rewrite(entries):
        calls["count"] += 1
        return False

    monkeypatch.setattr(usage_log, "_rewrite_file", failing_rewrite)
    for index in range(11):
        usage_log.record(_entry(ts=f"2026-10-17T10:00:{index:02d}"))
    assert calls["count"] == 1
    assert usage_log._file_lines == 0  # 失败后退避清零
    for index in range(11, 20):
        usage_log.record(_entry(ts=f"2026-10-17T10:00:{index:02d}"))
    assert calls["count"] == 1  # 退避期内不再重试
    assert usage_log.query()["total"] == 20


def test_non_dict_lines_skipped(isolated):
    path = isolated / "usage-log.jsonl"
    path.write_text(
        json.dumps(_entry(), ensure_ascii=False) + "\n"
        + "123\n"
        + "[]\n",
        encoding="utf-8",
    )
    usage_log.init(str(isolated))
    result = usage_log.query(start="2026-10-01", end="2026-10-31", daily=True)
    assert result["total"] == 1
    assert result["daily"] is not None


def test_record_ignores_non_dict(isolated, capsys):
    usage_log.record("not a dict")
    usage_log.record(None)
    assert usage_log.query()["total"] == 0
    assert "已忽略" in capsys.readouterr().out


def test_guards_ignore_non_dict_entries():
    assert usage_log._entry_date("2026-10-17") == ""
    summary, by_model = usage_log._aggregate([_entry(), "junk", None])
    assert summary["requests"] == 1
    assert by_model["glm-5.3-flash"]["requests"] == 1
    daily = usage_log._daily([_entry(ts="2026-10-17T10:00:00"), 123])
    assert len(daily) == 1
    assert daily[0]["date"] == "2026-10-17"


def test_keep_count_never_zero(isolated, monkeypatch):
    monkeypatch.setattr(usage_log, "_MAX_FILE_ENTRIES", 1)
    usage_log.record(_entry(ts="2026-10-17T10:00:00"))
    usage_log.record(_entry(ts="2026-10-17T10:00:01"))
    result = usage_log.query()
    assert result["total"] == 1  # 保留最新 1 条而非清空
    assert result["entries"][0]["ts"] == "2026-10-17T10:00:01"


def test_token_value_and_daily_enabled_edge_cases():
    assert usage_log._token_value({"input_tokens": "abc"}, "input_tokens") == 0
    assert usage_log._token_value({"input_tokens": None}, "input_tokens") == 0
    assert usage_log._token_value({"input_tokens": 7}, "input_tokens") == 7
    assert usage_log._daily_enabled(1) is True
    assert usage_log._daily_enabled(0) is False
    assert usage_log._daily_enabled(None) is False
    assert usage_log._daily_enabled(" TRUE ") is True
