# -*- coding: utf-8 -*-
"""
模型单价表与费用计算
====================
按管理员配置的单价表（CODEARTS_PRICING）把使用日志的 token 用量换算为
费用、Credits 与缓存节省额。单价单位为 USD / 1M tokens，键为上游模型 id
（resolve_model() 之后的 id）。

未配置单价的模型返回 None（界面显示「–」），不猜测价格。单价表非法时
整表视为空表并返回一条警告，代理主链路不受影响。
"""
import json
import math

import config_store

_PRICE_FIELDS = ("input", "output", "cache_read", "cache_creation")
_REQUIRED_FIELDS = ("input", "output")


def _valid_number(value) -> bool:
    """有限非负实数；布尔值不算数值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def load_table() -> tuple[dict, str]:
    """解析 CODEARTS_PRICING；返回 (单价表, 警告)。

    每次查询调用一次。整表非法（非 JSON / 非对象 / 含非法数值）返回空表
    与警告；单个模型缺 input/output 时跳过该模型（视为未配置）。
    """
    raw = (config_store.get("CODEARTS_PRICING", "") or "").strip()
    if not raw:
        return {}, ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as error:
        return {}, f"警告: CODEARTS_PRICING 不是合法 JSON（{error}），按空单价表处理"
    if not isinstance(data, dict):
        return {}, "警告: CODEARTS_PRICING 顶层必须是对象，按空单价表处理"
    table = {}
    for model, item in data.items():
        if not isinstance(model, str) or not model:
            return {}, "警告: CODEARTS_PRICING 的模型键必须是非空字符串，按空单价表处理"
        if not isinstance(item, dict):
            return {}, f"警告: CODEARTS_PRICING 中 {model} 的配置必须是对象，按空单价表处理"
        if any(field not in item for field in _REQUIRED_FIELDS):
            continue  # 缺必填单价 → 该模型视为未配置
        prices = {}
        for field in _PRICE_FIELDS:
            if field not in item:
                continue
            value = item[field]
            if not _valid_number(value):
                return {}, (f"警告: CODEARTS_PRICING 中 {model} 的 {field} "
                            "必须是非负有限数值，按空单价表处理")
            prices[field] = float(value)
        prices.setdefault("cache_read", prices["input"])
        prices.setdefault("cache_creation", prices["input"])
        table[model] = prices
    return table, ""


def load_rate() -> float:
    """读取 CODEARTS_CREDITS_RATE；缺失或非法时回退 1.0。"""
    raw = (config_store.get("CODEARTS_CREDITS_RATE", "1.0") or "").strip()
    try:
        rate = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(rate) or rate < 0:
        return 1.0
    return rate


def _tokens(entry: dict, key: str):
    """取 token 数：缺失按 0（历史条目兼容）；存在但非有限非负数返回 None。"""
    value = entry.get(key)
    if value is None:
        return 0.0
    if not _valid_number(value):
        return None
    return float(value)


def compute(entry: dict, table: dict, rate: float) -> dict | None:
    """计算单条记录的费用 / Credits / 缓存节省额；未计价或脏数据返回 None。"""
    try:
        if not isinstance(entry, dict):
            return None
        prices = table.get(entry.get("model"))
        if not isinstance(prices, dict):
            return None
        input_price = prices.get("input")
        output_price = prices.get("output")
        if not _valid_number(input_price) or not _valid_number(output_price):
            return None
        cache_read_price = prices.get("cache_read", input_price)
        cache_creation_price = prices.get("cache_creation", input_price)
        if not _valid_number(cache_read_price) or not _valid_number(cache_creation_price):
            return None
        input_tokens = _tokens(entry, "input_tokens")
        output_tokens = _tokens(entry, "output_tokens")
        cache_read_tokens = _tokens(entry, "cache_read_tokens")
        cache_creation_tokens = _tokens(entry, "cache_creation_tokens")
        if any(value is None for value in (
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens)):
            return None
        cost = (
            input_tokens * input_price
            + output_tokens * output_price
            + cache_read_tokens * cache_read_price
            + cache_creation_tokens * cache_creation_price
        ) / 1_000_000
        saved = max(
            0.0,
            cache_read_tokens * (input_price - cache_read_price) / 1_000_000,
        )
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or rate < 0):
            rate = 1.0
        return {"cost": cost, "credits": cost * rate, "saved": saved}
    except (KeyError, TypeError, ValueError):
        return None
