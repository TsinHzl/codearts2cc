# -*- coding: utf-8 -*-
"""
使用日志记录器（双端点共享）
============================
每次成功聊天补全请求收尾时记录一条 JSONL 日志（usage-log.jsonl），
内存侧维护全量聚合（总量 + 按 model 分组）与全量明细，供 admin
「使用日志」页按日期区间查询。启动时回放文件重建聚合；文件超过阈值时
原子重写保留较新一半（同 models-cache.json 的 tmp + os.replace 模式），
运行期写入同样在超阈值时滚动，避免内存无界增长。

仅记录元数据与用量（无消息内容、无密钥）；记录失败仅打印警告，
不影响代理主链路。
"""
import json
import os
import threading

_LOCK = threading.Lock()
# 文件滚动阈值：超过后保留较新一半
_MAX_FILE_ENTRIES = 10000
# 聚合口径的 token 字段（历史条目缺失按 0）
_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")

# 模块状态（init() 时初始化；未初始化时 record/query 为无害空操作）
_initialized = False
_path = ""
_entries = []            # 全量明细（旧 → 新，尾部为最新）
_file_lines = 0          # 文件中成功解析的条目数（滚动阈值依据）
_summary = {"requests": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0}
_by_model = {}           # model → {requests, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}


def _keep_count() -> int:
    """滚动后保留的条目数；至少 1，避免阈值极小时清空全部日志。"""
    return max(1, _MAX_FILE_ENTRIES // 2)


def _empty_bucket() -> dict:
    """新建聚合桶：请求数 + 各 token 累计。"""
    bucket = {"requests": 0}
    bucket.update({key: 0 for key in _TOKEN_KEYS})
    return bucket


def _token_value(entry: dict, key: str) -> int:
    """取 token 数并转为整数；脏数据按 0，避免查询与统计抛错。"""
    try:
        return int(entry.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _entry_date(entry: dict) -> str:
    """取记录日期（YYYY-MM-DD）；缺失或非字符串返回空串。"""
    if not isinstance(entry, dict):
        return ""
    ts = entry.get("ts")
    return ts[:10] if isinstance(ts, str) else ""


def _model_name(entry: dict) -> str:
    """取模型名；缺失或非字符串回退 unknown，避免非法类型作 dict 键。"""
    raw_model = entry.get("model")
    return raw_model if isinstance(raw_model, str) and raw_model else "unknown"


def _aggregate(entries: list) -> tuple[dict, dict]:
    """基于给定集合重建 (summary, by_model)。"""
    summary = _empty_bucket()
    by_model = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model = _model_name(entry)
        bucket = by_model.setdefault(model, _empty_bucket())
        bucket["requests"] += 1
        summary["requests"] += 1
        for key in _TOKEN_KEYS:
            value = _token_value(entry, key)
            bucket[key] += value
            summary[key] += value
    return summary, by_model


def _rewrite_file(entries: list) -> bool:
    """整文件原子重写；失败返回 False（调用方决定是否保留全量）。"""
    try:
        tmp_path = _path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(tmp_path, _path)
        return True
    except OSError as error:
        print(f"警告: usage-log.jsonl 滚动清理失败（{error}），本次保留全量")
        return False


def init(data_dir: str = "") -> None:
    """初始化：回放已有日志文件重建聚合，超阈值时滚动清理。"""
    global _initialized, _path, _entries, _file_lines, _summary, _by_model
    base = data_dir or os.path.dirname(os.path.abspath(__file__))
    _path = os.path.join(base, "usage-log.jsonl")
    entries = []
    if os.path.exists(_path):
        try:
            with open(_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parsed = None
                    parsed_ok = False
                    try:
                        parsed = json.loads(line)
                        parsed_ok = True
                    except json.JSONDecodeError:
                        pass
                    if parsed_ok:
                        if isinstance(parsed, dict):
                            entries.append(parsed)
                        else:
                            print("警告: usage-log.jsonl 存在非对象行，已跳过")
                        continue
                    # 崩溃残留的半行：按补右括号容错解析
                    try:
                        parsed = json.loads(line + "}")
                    except (json.JSONDecodeError, ValueError):
                        print(f"警告: usage-log.jsonl 存在无法解析的残损行，已跳过")
                        continue
                    if isinstance(parsed, dict):
                        entries.append(parsed)
                    else:
                        print("警告: usage-log.jsonl 存在非对象行，已跳过")
        except OSError as error:
            print(f"警告: usage-log.jsonl 读取失败（{error}），统计从零开始")
            entries = []
    # 启动滚动：超过阈值保留较新一半（整文件重写，tmp + os.replace 原子替换）
    if len(entries) > _MAX_FILE_ENTRIES:
        keep = entries[len(entries) - _keep_count():]
        if _rewrite_file(keep):
            entries = keep
            print(f"提示: usage-log.jsonl 超过 {_MAX_FILE_ENTRIES} 条，已滚动清理至 {len(entries)} 条")
    with _LOCK:
        _entries = entries
        _file_lines = len(entries)
        _summary, _by_model = _aggregate(entries)
        _initialized = True


def _apply_to_summary(entry: dict) -> None:
    """单条记录累加进全量聚合与按模型分组。"""
    model = _model_name(entry)
    bucket = _by_model.setdefault(model, _empty_bucket())
    bucket["requests"] += 1
    _summary["requests"] += 1
    for key in _TOKEN_KEYS:
        value = _token_value(entry, key)
        bucket[key] += value
        _summary[key] += value


def _rollback_locked() -> bool:
    """写锁内滚动：保留较新一半、原子重写、重建聚合（调用方已持锁）。

    重写失败返回 False，由调用方退避，避免每条记录都持锁重试整文件重写。
    """
    global _entries, _file_lines, _summary, _by_model
    keep = _entries[len(_entries) - _keep_count():]
    if not _rewrite_file(keep):
        return False
    _entries = keep
    _file_lines = len(keep)
    _summary, _by_model = _aggregate(keep)
    print(f"提示: usage-log.jsonl 运行期超过 {_MAX_FILE_ENTRIES} 条，已滚动清理至 {len(keep)} 条")
    return True


def record(entry: dict) -> None:
    """记录一条使用日志（双端点收尾调用；失败仅警告不影响主链路）。"""
    global _file_lines
    if not _initialized:
        return
    if not isinstance(entry, dict):
        print(f"警告: 使用日志条目类型非法（{type(entry).__name__}），已忽略")
        return
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            with open(_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _entries.append(dict(entry))
            _apply_to_summary(entry)
            _file_lines += 1
            if _file_lines > _MAX_FILE_ENTRIES and not _rollback_locked():
                # 重写失败：清零计数器退避，避免后续每条记录都持锁重试整文件重写
                _file_lines = 0
    except Exception as error:  # 记录失败绝不影响代理主链路
        print(f"警告: 使用日志写入失败: {error}")


def _daily(entries: list) -> list:
    """按日期聚合（升序）；缺失 ts 的记录不参与。"""
    buckets = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date = _entry_date(entry)
        if not date:
            continue
        bucket = buckets.get(date)
        if bucket is None:
            bucket = {"date": date, "requests": 0}
            bucket.update({key: 0 for key in _TOKEN_KEYS})
            buckets[date] = bucket
        bucket["requests"] += 1
        for key in _TOKEN_KEYS:
            bucket[key] += _token_value(entry, key)
    return [buckets[date] for date in sorted(buckets)]


def _daily_by_model(entries: list) -> list:
    """按 (日期, 模型) 聚合（日期、模型均升序）；缺失 ts 的记录不参与。"""
    buckets = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date = _entry_date(entry)
        if not date:
            continue
        model = _model_name(entry)
        bucket = buckets.get((date, model))
        if bucket is None:
            bucket = {"date": date, "model": model, "requests": 0}
            bucket.update({key: 0 for key in _TOKEN_KEYS})
            buckets[(date, model)] = bucket
        bucket["requests"] += 1
        for key in _TOKEN_KEYS:
            bucket[key] += _token_value(entry, key)
    return [buckets[key] for key in sorted(buckets)]


def _daily_enabled(daily) -> bool:
    """daily 开关：布尔真值或 "true"/"1"（大小写不敏感）为开启。"""
    if isinstance(daily, bool):
        return daily
    if isinstance(daily, int):
        return daily == 1
    if isinstance(daily, str):
        return daily.strip().lower() in ("true", "1")
    return False


def query(page: int = 1, limit: int = 50, start: str = "", end: str = "", daily=False) -> dict:
    """返回过滤后的统计、明细页与可选每日聚合（明细按时间倒序）。

    start / end 为 YYYY-MM-DD（由调用方校验）；同时为空时不过滤。
    """
    if not _initialized:
        return {"summary": dict(_summary), "by_model": {}, "entries": [],
                "page": page, "limit": limit, "total": 0, "daily": None,
                "daily_models": None}
    limit = max(1, min(int(limit or 50), 200))
    page = max(1, int(page or 1))
    with _LOCK:
        if start or end:
            filtered = []
            for entry in _entries:
                date = _entry_date(entry)
                if not date or (start and date < start) or (end and date > end):
                    continue
                filtered.append(entry)
            summary, by_model = _aggregate(filtered)
        else:
            filtered = _entries
            summary = dict(_summary)
            by_model = {model: dict(bucket) for model, bucket in _by_model.items()}
        total = len(filtered)
        begin = max(0, total - page * limit)
        finish = max(0, total - (page - 1) * limit)
        entries_page = [dict(entry) for entry in reversed(filtered[begin:finish])]
        daily_enabled = _daily_enabled(daily)
        return {
            "summary": summary,
            "by_model": by_model,
            "entries": entries_page,
            "page": page,
            "limit": limit,
            "total": total,
            "daily": _daily(filtered) if daily_enabled else None,
            "daily_models": _daily_by_model(filtered) if daily_enabled else None,
        }
