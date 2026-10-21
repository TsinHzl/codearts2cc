# -*- coding: utf-8 -*-
"""
模型单价表与费用计算
====================
按管理员配置的单价表（CODEARTS_PRICING）把使用日志的 token 用量换算为
费用、Credits 与缓存节省额。单价单位为 USD / 1M tokens，键为上游模型 id
（resolve_model() 之后的 id）。

未配置单价的模型返回 None（界面显示「–」），不猜测价格。单价表非法时
整表视为空表并返回一条警告，代理主链路不受影响。

单价表由官方单价表（随模型刷新落盘于 models-cache.json）与管理员配置的
CODEARTS_PRICING 合并而成：配置优先，未覆盖的模型回退官方单价。
"""
import json
import math

from src import paths
from src.config import config_store

_PRICE_FIELDS = ("input", "output", "cache_read", "cache_creation")
_REQUIRED_FIELDS = ("input", "output")
_DEFAULT_TIME_SLOTS = ("", "default")


def pick_default_credit_tier(credits):
    """从官方 credit 档位数组取 time_slot_id 为 null/空/default 的首档；无匹配返回 None。"""
    if not isinstance(credits, list):
        return None
    for tier in credits:
        if not isinstance(tier, dict):
            continue
        slot = tier.get("time_slot_id")
        if slot is None or str(slot).strip().lower() in _DEFAULT_TIME_SLOTS:
            return tier
    return None


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


def _cache_path(cache_path) -> str:
    """官方单价来源路径：显式传入优先，缺省取项目内 models-cache.json。"""
    return cache_path or paths.MODEL_CACHE_PATH


def _price_from_text(value):
    """把官方价字符串转为有限非负数；非法返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if _valid_number(value) else None
    if not isinstance(value, str):
        return None
    try:
        number = float(value.strip())
    except (TypeError, ValueError):
        return None
    return number if _valid_number(number) else None


def load_official_table(cache_path=None) -> dict:
    """从模型缓存读取官方单价并归一化为与 CODEARTS_PRICING 同构的单价表。

    缓存缺失 / 非法 / 条目无 credit_pricing 时返回空表；单个模型的输入或输出
    单价非法时跳过该模型（视为未配置），不影响其他模型。
    """
    try:
        with open(_cache_path(cache_path), "r", encoding="utf-8") as file:
            cached = json.load(file)
    except (OSError, ValueError, TypeError):
        return {}
    models = cached.get("models") if isinstance(cached, dict) else None
    if not isinstance(models, list):
        return {}
    table = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        official = item.get("credit_pricing")
        if not isinstance(model_id, str) or not model_id or not isinstance(official, dict):
            continue
        input_price = _price_from_text(official.get("input_price"))
        output_price = _price_from_text(official.get("output_price"))
        cache_price = _price_from_text(official.get("cache_price"))
        if input_price is None or output_price is None:
            continue
        if cache_price is None:
            cache_price = input_price
        table[model_id] = {
            "input": input_price,
            "output": output_price,
            "cache_read": cache_price,
            "cache_creation": cache_price,
        }
    return table


def load_merged_table(cache_path=None) -> tuple[dict, str]:
    """合并单价表：官方表为底，CODEARTS_PRICING 覆盖同名模型。

    返回 (单价表, 警告)；警告优先取手工表解析警告，官方表读取失败静默忽略。
    """
    manual, warning = load_table()
    merged = load_official_table(cache_path)
    merged.update(manual)
    return merged, warning


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
