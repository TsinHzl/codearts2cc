# -*- coding: utf-8 -*-
"""
使用日志记录器（双端点共享）
============================
每次成功聊天补全请求收尾时记录一条 JSONL 日志（usage-log.jsonl），
内存侧维护全量聚合（总量 + 按 model 分组）与最近 N 条明细，供
admin「使用日志」页查询。启动时回放文件重建聚合，文件超过阈值时
原子重写保留较新一半（同 models-cache.json 的 tmp + os.replace 模式）。

仅记录元数据与用量（无消息内容、无密钥）；记录失败仅打印警告，
不影响代理主链路。
"""
import json
import os
import threading

_LOCK = threading.Lock()
# 内存明细保留条数（展示用；统计走全量聚合，不受此限）
_MAX_ENTRIES = 200
# 文件滚动阈值：超过后启动时保留较新一半
_MAX_FILE_ENTRIES = 10000

# 模块状态（init() 时初始化；未初始化时 record/query 为无害空操作）
_initialized = False
_path = ""
_entries = []            # 最近明细（旧 → 新，尾部为最新）
_summary = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
_by_model = {}           # model → {requests, input_tokens, output_tokens, cache_read_tokens}


def init(data_dir: str = "") -> None:
    """初始化：回放已有日志文件重建聚合，超阈值时滚动清理。"""
    global _initialized, _path, _entries, _summary, _by_model
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
                    try:
                        entries.append(json.loads(line))
                        continue
                    except json.JSONDecodeError:
                        pass
                    # 崩溃残留的半行：按补右括号容错解析
                    try:
                        entries.append(json.loads(line + "}"))
                    except (json.JSONDecodeError, ValueError):
                        print(f"警告: usage-log.jsonl 存在无法解析的残损行，已跳过")
        except OSError as error:
            print(f"警告: usage-log.jsonl 读取失败（{error}），统计从零开始")
            entries = []
    # 滚动清理：超过阈值保留较新一半（整文件重写，tmp + os.replace 原子替换）
    if len(entries) > _MAX_FILE_ENTRIES:
        keep = entries[len(entries) - _MAX_FILE_ENTRIES // 2:]
        try:
            tmp_path = _path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                for entry in keep:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.replace(tmp_path, _path)
            entries = keep
            print(f"提示: usage-log.jsonl 超过 {_MAX_FILE_ENTRIES} 条，已滚动清理至 {len(entries)} 条")
        except OSError as error:
            # 清理失败不阻断启动，保留全量继续服务
            print(f"警告: usage-log.jsonl 滚动清理失败（{error}），本次保留全量")
    with _LOCK:
        _entries = entries[-_MAX_ENTRIES:]
        _summary = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
        _by_model = {}
        for entry in entries:
            _apply_to_summary(entry)
        _initialized = True


def _apply_to_summary(entry: dict) -> None:
    """单条记录累加进全量聚合与按模型分组。"""
    model = entry.get("model") or "unknown"
    bucket = _by_model.setdefault(
        model, {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}
    )
    bucket["requests"] += 1
    _summary["requests"] += 1
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        value = int(entry.get(key) or 0)
        bucket[key] += value
        _summary[key] += value


def record(entry: dict) -> None:
    """记录一条使用日志（双端点收尾调用；失败仅警告不影响主链路）。"""
    if not _initialized:
        return
    try:
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            with open(_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            _entries.append(entry)
            if len(_entries) > _MAX_ENTRIES:
                del _entries[: len(_entries) - _MAX_ENTRIES]
            _apply_to_summary(entry)
    except Exception as error:  # 记录失败绝不影响代理主链路
        print(f"警告: 使用日志写入失败: {error}")


def query(page: int = 1, limit: int = 50) -> dict:
    """返回 {summary, by_model, entries, page, limit, total}（明细按时间倒序）。"""
    if not _initialized:
        return {"summary": dict(_summary), "by_model": {}, "entries": [], "page": page, "limit": limit, "total": 0}
    limit = max(1, min(int(limit or 50), 200))
    page = max(1, int(page or 1))
    with _LOCK:
        total = len(_entries)
        start = max(0, total - page * limit)
        end = max(0, total - (page - 1) * limit)
        entries_page = list(reversed(_entries[start:end]))
        return {
            "summary": dict(_summary),
            "by_model": {model: dict(bucket) for model, bucket in _by_model.items()},
            "entries": entries_page,
            "page": page,
            "limit": limit,
            "total": total,
        }
