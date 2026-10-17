# -*- coding: utf-8 -*-
"""
前缀缓存模拟（借鉴 kiro2cc-proxy 的分层方案）
=============================================
CodeArts 上游不返回缓存计量且按普通 token 全额计费；本模块在代理侧
模拟 Anthropic prompt cache 的 usage 字段（cache_read/cache_creation），
使 Claude Code 的上下文统计与费用预估贴近真实缓存语义。

派生链（镜像 kiro2cc-proxy cache::select_final_usage，省去 metering/ratio 层）：
  1. 指纹命中 → cache_read = 匹配前缀 token（封顶 85%），余量计入 cache_creation
  2. 前缀兜底（首请求/前缀完全分歧）→ 前缀（system+tools+历史消息）计入 cache_creation

不变式：cache_read + cache_creation + 非缓存 input == total_input。

配置开关（config.json）：CODEARTS_CACHE_SIM=0 关闭（默认开启）。
"""
import hashlib
import json
import threading
import time

import config_store

# 指纹 TTL（秒），与 Anthropic ephemeral cache 的 5m 档对齐
TTL_5M = 5 * 60
# cache_read 封顶比例：Anthropic 协议要求至少保留一段非缓存前缀
CACHE_READ_CAP_RATIO = 0.85
# 单账号最多保留的指纹段数（超出截断尾部，保持前缀单调性）
MAX_BREAKPOINTS = 256


def cache_enabled() -> bool:
    return config_store.get("CODEARTS_CACHE_SIM", "1").strip() != "0"


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：len//4（与 /v1/messages/count_tokens 同口径）。"""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _segment_reprs(body: dict) -> list:
    """请求体 → 有序段表示列表（S:system / T:tools / M:role:content）。

    tools 集变化会使 T 段及其后全部段哈希失效（与 kiro2cc 同语义）。
    """
    segments = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        segments.append("S:" + system.strip())
    elif isinstance(system, list):
        text = "\n".join(
            b.get("text", "").strip() for b in system
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if text:
            segments.append("S:" + text)

    tools = body.get("tools") or []
    if isinstance(tools, list):
        parts = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            schema = json.dumps(
                tool.get("input_schema") or {}, ensure_ascii=False, sort_keys=True
            )
            parts.append(f"{tool.get('name', '')}:{tool.get('description', '')}:{schema}")
        if parts:
            segments.append("T:" + "\x1f".join(parts))

    messages = body.get("messages")
    if not isinstance(messages, list):
        messages = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        content = message.get("content")
        if isinstance(content, str):
            repr_text = content.strip()
        else:
            parts = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", "").strip())
                elif btype == "tool_use":
                    input_json = json.dumps(
                        block.get("input") or {}, ensure_ascii=False, sort_keys=True
                    )
                    parts.append(f"tool_use:{block.get('name', '')}:{input_json}")
                elif btype == "tool_result":
                    inner = block.get("content")
                    inner_text = inner if isinstance(inner, str) else json.dumps(
                        inner or "", ensure_ascii=False
                    )
                    parts.append(f"tool_result:{block.get('tool_use_id', '')}:{inner_text.strip()}")
                elif btype == "image":
                    # base64 载荷粗估计入（len//3：base64 膨胀 ~4/3，图像 token 密度低于文本）
                    data = (block.get("source") or {}).get("data") or ""
                    parts.append(f"image:{len(data) // 3}")
                elif btype == "thinking":
                    # thinking 块内容不影响前缀语义，仅记类型保持段稳定
                    parts.append("thinking")
                else:
                    parts.append(btype or "unknown")
            repr_text = "\x1f".join(parts)
        segments.append(f"M:{role}:{repr_text}")
    return segments


def build_profile(body: dict) -> list:
    """请求体 → 累积 SHA-256 指纹链（hash[k] = SHA(seg[0]..seg[k])）。

    累积哈希保证前缀单调性：若第 k 段命中则 0..k 必命中。
    """
    profile = []
    hasher = hashlib.sha256()
    cumulative_tokens = 0
    for seg in _segment_reprs(body):
        hasher = hasher.copy()
        hasher.update(seg.encode("utf-8"))
        cumulative_tokens += estimate_tokens(seg)
        profile.append({
            "hash": hasher.hexdigest(),
            "cumulative_tokens": cumulative_tokens,
        })
    return profile


def estimate_body_tokens(body: dict) -> int:
    """整条请求的估算 token 总数（流式路径在 usage 未知时作 total 基准）。"""
    return sum(estimate_tokens(seg) for seg in _segment_reprs(body))


def estimate_prefix_tokens(body: dict) -> int:
    """前缀 token 估算：system + tools + messages[:-1]（最后一条消息不进缓存）。

    无消息时 system/tools 全部视为前缀。
    """
    segments = _segment_reprs(body)
    if not body.get("messages"):
        return sum(estimate_tokens(seg) for seg in segments)
    return sum(estimate_tokens(seg) for seg in segments[:-1])


class FingerprintTracker:
    """账号级前缀指纹表（线程安全；Flask threaded 模式下多请求并发）。

    账号维度：本代理无多账号，统一用 "default"，接口保留 account_id。
    过期/淘汰统一 5m 档：累积哈希链 + 前缀截断语义下，「头部过期、
    深部 1h 存活」无法表达（截断会把 1h 段一并清掉，tier 字段成死
    代码），故不做 5m/1h 分档，语义与真实 Anthropic cache 有意保持差异。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # account_id → list[breakpoint]；breakpoint = {hash, cumulative_tokens, last_hit_at}
        self._tables = {}

    def _evict_expired_locked(self, table: list, now: float) -> None:
        """从链头起保留未过期前缀，首个过期段起整体截断。

        累积哈希依赖前缀单调，不能逐段独立过滤——中途挖掉一段会让
        后续段的哈希失去比对意义。
        """
        kept = 0
        for bp in table:
            if now - bp["last_hit_at"] > TTL_5M:
                break
            kept += 1
        del table[kept:]

    def compute(self, account_id: str, profile: list, total_input: int):
        """比对指纹链，命中返回 (cache_read, cache_creation, 非缓存 0)。

        零命中/关闭/空 profile 返回 None，由调用方走前缀兜底。
        命中时余量全部计入 cache_creation（指纹层无法区分新后缀与
        最后一条消息，语义为「本前缀在 5m 内已被上游处理过一次」）。
        """
        if not cache_enabled() or not profile or total_input <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            table = self._tables.get(account_id, [])
            self._evict_expired_locked(table, now)
            matched = 0
            for seg, bp in zip(profile, table):
                if bp["hash"] != seg["hash"]:
                    break
                bp["last_hit_at"] = now
                matched = seg["cumulative_tokens"]
        if matched <= 0:
            return None
        cache_read = min(matched, int(total_input * CACHE_READ_CAP_RATIO))
        return (cache_read, max(0, total_input - cache_read), 0)

    def update(self, account_id: str, profile: list) -> None:
        """请求完成后回写指纹表：命中前缀刷新时间戳，新段追加并截断超限尾部。"""
        if not cache_enabled() or not profile:
            return
        now = time.monotonic()
        with self._lock:
            table = self._tables.setdefault(account_id, [])
            matched = 0
            limit = min(len(profile), len(table))
            while matched < limit and table[matched]["hash"] == profile[matched]["hash"]:
                table[matched]["last_hit_at"] = now
                matched += 1
            # 截断失配尾部（累积哈希依赖前缀单调，只能保留前缀段）
            del table[matched:]
            for seg in profile[matched:]:
                table.append({
                    "hash": seg["hash"],
                    "cumulative_tokens": seg["cumulative_tokens"],
                    "last_hit_at": now,
                })
            if len(table) > MAX_BREAKPOINTS:
                del table[MAX_BREAKPOINTS:]


# 进程级单例（server.py 启动即存在；代理无多 worker，无需跨进程共享）
tracker = FingerprintTracker()


def select_cache_usage(total_input: int, body: dict, commit: bool = True):
    """主入口：返回 (cache_read, cache_creation, 非缓存 input) 注入响应。

    派生链：指纹命中 → 前缀兜底。关闭/total<=0 时返回 None
    （响应不带缓存字段）。不变式：三者之和 == total_input。

    commit=True 时回写指纹表；仅查询传 commit=False（由调用方在
    上游成功后显式 tracker.update，避免失败请求污染指纹表）。
    """
    if not cache_enabled():
        return None
    total = max(0, int(total_input or 0))
    if total <= 0:
        return None

    segments = _segment_reprs(body)  # 全函数只切分一次
    profile = _build_profile_from_segments(segments)

    fingerprint = tracker.compute("default", profile, total)
    if fingerprint is not None:
        usage = fingerprint
    else:
        # 前缀兜底：首请求或前缀分歧，可缓存前缀全部计入 cache_creation
        prefix = segments[:-1] if body.get("messages") else segments
        creation = min(sum(estimate_tokens(seg) for seg in prefix), total)
        usage = (0, creation, total - creation)

    if commit:
        tracker.update("default", profile)
    return usage


def _build_profile_from_segments(segments: list) -> list:
    """段列表 → 累积 SHA-256 指纹链（build_profile 的 segments 版）。"""
    profile = []
    hasher = hashlib.sha256()
    cumulative_tokens = 0
    for seg in segments:
        hasher = hasher.copy()
        hasher.update(seg.encode("utf-8"))
        cumulative_tokens += estimate_tokens(seg)
        profile.append({
            "hash": hasher.hexdigest(),
            "cumulative_tokens": cumulative_tokens,
        })
    return profile
