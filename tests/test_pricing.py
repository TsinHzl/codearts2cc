# -*- coding: utf-8 -*-
"""pricing 单元测试：单价表解析与费用计算。"""
import json

import pytest

from src.config import config_store
from src.usage import pricing


@pytest.fixture()
def config(monkeypatch):
    """用内存字典替换 config_store.get，避免触碰真实 config.json。"""
    values = {}
    monkeypatch.setattr(
        config_store, "get",
        lambda key, default="": values.get(key, default),
    )
    return values


def _table(**overrides):
    item = {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_creation": 0.5}
    item.update(overrides)
    return {"glm-5.3-flash": item}


def _entry(**overrides):
    entry = {
        "model": "glm-5.3-flash",
        "input_tokens": 1_000_000,
        "output_tokens": 500_000,
        "cache_read_tokens": 2_000_000,
        "cache_creation_tokens": 1_000_000,
    }
    entry.update(overrides)
    return entry


def test_compute_hit_all_fields():
    result = pricing.compute(_entry(), _table(), 2.0)
    assert result == pytest.approx({"cost": 2.7, "credits": 5.4, "saved": 1.8})


def test_compute_unknown_model_returns_none():
    assert pricing.compute(_entry(model="other"), _table(), 1.0) is None


def test_load_table_invalid_json(config):
    config["CODEARTS_PRICING"] = "{not json"
    table, warning = pricing.load_table()
    assert table == {}
    assert "JSON" in warning


def test_load_table_negative_price(config):
    config["CODEARTS_PRICING"] = '{"m": {"input": -1, "output": 1}}'
    table, warning = pricing.load_table()
    assert table == {}
    assert warning


def test_load_table_nan_price(config):
    config["CODEARTS_PRICING"] = '{"m": {"input": NaN, "output": 1}}'
    table, warning = pricing.load_table()
    assert table == {}
    assert warning


def test_load_table_infinity_price(config):
    config["CODEARTS_PRICING"] = '{"m": {"input": Infinity, "output": 1}}'
    table, warning = pricing.load_table()
    assert table == {}
    assert warning


def test_load_table_boolean_price(config):
    config["CODEARTS_PRICING"] = '{"m": {"input": true, "output": 1}}'
    table, warning = pricing.load_table()
    assert table == {}
    assert warning


def test_load_table_cache_prices_default_to_input(config):
    config["CODEARTS_PRICING"] = '{"m": {"input": 3.0, "output": 6.0}}'
    table, warning = pricing.load_table()
    assert warning == ""
    assert table["m"]["cache_read"] == 3.0
    assert table["m"]["cache_creation"] == 3.0


def test_compute_saved_never_negative():
    table = _table(cache_read=5.0)  # 缓存单价高于输入单价
    result = pricing.compute(_entry(), table, 1.0)
    assert result["saved"] == 0.0


def test_upstream_model_id_key(config):
    """单价表键为上游模型 id，compute 精确匹配不做别名映射。"""
    config["CODEARTS_PRICING"] = '{"openpangu-2.0-pro": {"input": 1.0, "output": 2.0}}'
    table, warning = pricing.load_table()
    assert warning == ""
    result = pricing.compute(
        {"model": "openpangu-2.0-pro", "input_tokens": 1_000_000,
         "output_tokens": 0, "cache_read_tokens": 0},
        table, 1.0,
    )
    assert result["cost"] == pytest.approx(1.0)
    assert pricing.compute(_entry(model="openpangu-2.0-pro"), table, 1.0) is not None


def test_compute_missing_cache_creation_tokens_as_zero():
    entry = _entry()
    del entry["cache_creation_tokens"]
    result = pricing.compute(entry, _table(), 1.0)
    assert result["cost"] == pytest.approx(2.2)  # 1.0 + 1.0 + 0.2 + 0


def test_compute_dirty_tokens_returns_none():
    assert pricing.compute(_entry(input_tokens="abc"), _table(), 1.0) is None
    assert pricing.compute(_entry(output_tokens=True), _table(), 1.0) is None
    assert pricing.compute(_entry(cache_read_tokens=float("nan")), _table(), 1.0) is None


def test_load_table_skips_model_missing_required(config):
    config["CODEARTS_PRICING"] = (
        '{"a": {"input": 1}, "b": {"input": 1, "output": 2}}'
    )
    table, warning = pricing.load_table()
    assert warning == ""
    assert "a" not in table
    assert table["b"]["input"] == 1.0


def test_load_table_top_level_not_object(config):
    config["CODEARTS_PRICING"] = "[]"
    table, warning = pricing.load_table()
    assert table == {}
    assert "对象" in warning


def test_load_table_huge_integer_price(config):
    config["CODEARTS_PRICING"] = '{"m": {"input": ' + "9" * 400 + ', "output": 1}}'
    table, warning = pricing.load_table()
    assert table == {}
    assert warning
    assert pricing.compute(_entry(input_tokens=10 ** 400), _table(), 1.0) is None


def test_load_rate_fallback(config):
    assert pricing.load_rate() == 1.0  # 缺失
    config["CODEARTS_CREDITS_RATE"] = "2.5"
    assert pricing.load_rate() == 2.5
    for raw in ("abc", "-1", "NaN", "Infinity"):
        config["CODEARTS_CREDITS_RATE"] = raw
        assert pricing.load_rate() == 1.0


# ---------------------------------------------------------------------------
# 官方单价表（models-cache.json → 单价表）
# ---------------------------------------------------------------------------

def _write_cache(path, models):
    path.write_text(json.dumps({"updated_at": 0, "models": models}), encoding="utf-8")
    return str(path)


def test_load_official_table_normalizes(tmp_path):
    """官方原始字段归一化为与 CODEARTS_PRICING 同构的单价表。"""
    cache = _write_cache(tmp_path / "models-cache.json", [
        {"id": "deepseek-v4.1-flash",
         "credit_pricing": {"input_price": "2", "cache_price": "0.2",
                            "output_price": "8", "time_slot_id": "default"}},
        {"id": "GLM-5.2"},  # 无 credit_pricing → 跳过
    ])
    table = pricing.load_official_table(cache)
    assert table == {"deepseek-v4.1-flash": {
        "input": 2.0, "output": 8.0, "cache_read": 0.2, "cache_creation": 0.2}}


def test_load_official_table_parses_string_and_numeric(tmp_path):
    """字符串与数值两种价格形态均可解析。"""
    cache = _write_cache(tmp_path / "models-cache.json", [
        {"id": "a", "credit_pricing": {"input_price": " 3.5 ", "cache_price": 1,
                                       "output_price": "14"}},
    ])
    table = pricing.load_official_table(cache)
    assert table["a"] == {"input": 3.5, "output": 14.0,
                          "cache_read": 1.0, "cache_creation": 1.0}


@pytest.mark.parametrize("bad", ["-1", "NaN", "Infinity", "abc", True, None])
def test_load_official_table_skips_invalid_price(tmp_path, bad):
    """输入/输出单价非法时跳过该模型，不影响其他模型。"""
    cache = _write_cache(tmp_path / "models-cache.json", [
        {"id": "bad", "credit_pricing": {"input_price": bad, "output_price": "1"}},
        {"id": "good", "credit_pricing": {"input_price": "1", "output_price": "2"}},
    ])
    table = pricing.load_official_table(cache)
    assert "bad" not in table
    assert table["good"]["input"] == 1.0


def test_load_merged_table_manual_overrides_official(tmp_path, config):
    """同名模型以 CODEARTS_PRICING 为准，未覆盖的模型回退官方单价。"""
    cache = _write_cache(tmp_path / "models-cache.json", [
        {"id": "a", "credit_pricing": {"input_price": "1", "cache_price": "0.1",
                                       "output_price": "2"}},
        {"id": "b", "credit_pricing": {"input_price": "5", "cache_price": "0.5",
                                       "output_price": "10"}},
    ])
    config["CODEARTS_PRICING"] = '{"a": {"input": 9.0, "output": 99.0}}'
    table, warning = pricing.load_merged_table(cache)
    assert warning == ""
    assert table["a"] == {"input": 9.0, "output": 99.0,
                          "cache_read": 9.0, "cache_creation": 9.0}
    assert table["b"]["input"] == 5.0


def test_load_official_table_missing_cache_returns_empty(tmp_path, config):
    """缓存缺失或结构异常时官方表为空，合并结果等于手工表。"""
    missing = str(tmp_path / "nope.json")
    assert pricing.load_official_table(missing) == {}
    (tmp_path / "broken.json").write_text("[]", encoding="utf-8")
    assert pricing.load_official_table(str(tmp_path / "broken.json")) == {}
    config["CODEARTS_PRICING"] = '{"m": {"input": 1.0, "output": 2.0}}'
    table, _ = pricing.load_merged_table(missing)
    assert set(table) == {"m"}


def test_pick_default_credit_tier_selects_null_or_default():
    """多档位取 time_slot_id 为 null 或 default 的首档。"""
    tiers = [
        {"time_slot_id": "off_peak", "input_price": "1"},
        {"time_slot_id": "default", "input_price": "2"},
        {"time_slot_id": None, "input_price": "3"},
    ]
    assert pricing.pick_default_credit_tier(tiers)["input_price"] == "2"
    assert pricing.pick_default_credit_tier([{"time_slot_id": None}])["time_slot_id"] is None
    assert pricing.pick_default_credit_tier([]) is None
    assert pricing.pick_default_credit_tier("oops") is None


def test_pick_default_credit_tier_without_default_is_unpriced():
    """全部档位均非 null/default 时视为未计价。"""
    tiers = [{"time_slot_id": "off_peak"}, {"time_slot_id": "peak"}]
    assert pricing.pick_default_credit_tier(tiers) is None
