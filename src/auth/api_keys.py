# -*- coding: utf-8 -*-
"""
API Key 存储与校验（本地代理鉴权）
==================================
以 api-keys.json 为唯一持久化载体，为 /v1/* 数据面提供本地 API Key
鉴权与自助管理能力。key 明文只落该文件（0600，不入库），不写日志、
不进异常堆栈。

结构：列表 [{id, key, name, enabled, createdAt}]（按 id 升序落盘）。

⚠️ id 分配约束：当前用「列表内最大 id + 1」，未持久化历史最大 id。
本模块不存储按 key 的用量统计，故删除 key 后 id 被复用暂无副作用。
**将来引入按 key 用量统计（如按 api_key_id 累计消费）前，必须先加
id 计数器文件**，否则新 key 会复用已删除 key 的 id 并继承其历史用量。

写盘顺序约束：先以 0600 建 tmp 文件再写入、最后 os.replace —— 不可在
replace 之后才 chmod，否则会留下一个明文文件为 umask 默认权限（通常
0644）的可读窗口。同 server.py 的 .admin_salt 与 config_store 的写法。
"""
import hmac
import json
import os
import secrets
import threading
import time

# 名称长度上限（管理端点与存储层共用同一口径）
NAME_MAX_LEN = 64

_path = ""                       # api-keys.json 绝对路径（init 注入）
_keys = []                       # 内存态：按 id 升序的 key 列表
_initialized = False
_lock = threading.Lock()         # 读-改-写互斥（Flask threaded=True）


def init(path: str) -> None:
    """加载 api-keys.json 到内存；文件不存在视为空列表，损坏则降级并告警。

    与 config_store.init 不同：本模块的损坏降级不退出进程——key 文件
    损坏不应让代理整体不可用，按空列表继续（届时所有请求 401，用户可
    在管理页面重建），比拒绝启动更可恢复。
    """
    global _path, _keys, _initialized
    with _lock:
        _path = path
        _keys = []
        _initialized = True
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            print(f"警告: api-keys.json 读取失败（{error}），已按空列表处理，请在管理页面重新生成 API Key")
            return
        if not isinstance(raw, list):
            print("警告: api-keys.json 顶层必须是数组，已按空列表处理，请在管理页面重新生成 API Key")
            return
        entries = []
        for item in raw:
            normalized = _normalize_entry(item)
            if normalized is None:
                print("警告: api-keys.json 存在结构非法的条目，已跳过")
                continue
            entries.append(normalized)
        entries.sort(key=lambda entry: entry["id"])
        _keys = entries
    try:
        os.chmod(path, 0o600)  # 含 key 明文，兜底收紧权限
    except OSError:
        pass


def _normalize_entry(item) -> dict | None:
    """把磁盘条目归一为内部结构；结构非法返回 None（调用方跳过并告警）。"""
    if not isinstance(item, dict):
        return None
    key = item.get("key")
    name = item.get("name")
    key_id = item.get("id")
    if not isinstance(key, str) or not key:
        return None
    if not isinstance(name, str):
        return None
    if not isinstance(key_id, int) or isinstance(key_id, bool) or key_id <= 0:
        return None
    created_at = item.get("createdAt")
    # 只接受真正的布尔：字符串 "false" / "0" 经 bool() 会被误判为 True
    enabled = item.get("enabled", True)
    return {
        "id": key_id,
        "key": key,
        "name": name,
        "enabled": enabled if isinstance(enabled, bool) else True,
        "createdAt": created_at if isinstance(created_at, str) else "",
    }


def list_keys() -> list:
    """返回全部 key 的深拷贝（按 id 升序），避免调用方原地修改内存态。"""
    with _lock:
        return [dict(entry) for entry in _keys]


def _write_locked(entries: list) -> None:
    """原子落盘（调用方已持锁）；失败抛出异常由调用方回滚。"""
    tmp_path = _path + ".tmp"
    payload = json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    # 先以 0600 建 tmp，杜绝 replace 前存在 0644 明文窗口
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, _path)
    except Exception:
        # 写盘或 replace 任一步失败都要清掉含明文的 tmp
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _next_id(entries: list) -> int:
    """下一个 id = 当前最大 id + 1（id 从 1 起，0 保留）。

    见模块 docstring 的 id 分配约束：引入按 key 用量统计前必须先加计数器。
    """
    return max((entry["id"] for entry in entries), default=0) + 1


def create_key(name: str) -> dict:
    """新建并持久化一个 key（默认启用），返回含明文的完整条目。"""
    if not _initialized:
        raise RuntimeError("api_keys 未初始化")
    with _lock:
        entry = {
            "id": _next_id(_keys),
            "key": "sk-" + secrets.token_hex(16),
            "name": name,
            "enabled": True,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        updated = _keys + [entry]
        _write_locked(updated)
        _keys[:] = updated
        return dict(entry)


def update_key(key_id: int, name=None, enabled=None) -> dict | None:
    """更新名称 / 启用态；id 不存在返回 None。未提供的字段保持不变。"""
    if not _initialized:
        raise RuntimeError("api_keys 未初始化")
    with _lock:
        updated = []
        target = None
        for entry in _keys:
            if entry["id"] == key_id:
                target = dict(entry)
                if isinstance(name, str):
                    target["name"] = name
                if isinstance(enabled, bool):
                    target["enabled"] = enabled
                updated.append(target)
            else:
                updated.append(dict(entry))
        if target is None:
            return None
        _write_locked(updated)
        _keys[:] = updated
        return dict(target)


def delete_key(key_id: int) -> bool:
    """删除指定 key；存在并已删除返回 True，id 不存在返回 False。"""
    if not _initialized:
        raise RuntimeError("api_keys 未初始化")
    with _lock:
        remaining = [dict(entry) for entry in _keys if entry["id"] != key_id]
        if len(remaining) == len(_keys):
            return False
        _write_locked(remaining)
        _keys[:] = remaining
        return True


# authenticate 的返回状态
AUTH_VALID = "valid"
AUTH_DISABLED = "disabled"
AUTH_NOT_FOUND = "not_found"


def authenticate(candidate: object) -> tuple[str, dict | None]:
    """校验候选 key，返回 (状态, 条目拷贝)；未匹配时条目为 None。

    恒定时间比较：遍历全部条目累积结果、不做短路返回，避免比较耗时随
    匹配前缀长度变化构成时序侧信道。候选与条目 key 统一按 UTF-8 编码为
    bytes 后再比较——hmac.compare_digest 对 str 入参要求 ASCII-only，
    而 Werkzeug 按 latin-1 解码请求头，非 ASCII 字节会让 str 形态直接抛
    TypeError（把 401 变成 500）。
    """
    if not isinstance(candidate, str) or not candidate:
        return AUTH_NOT_FOUND, None
    candidate_bytes = candidate.encode("utf-8", errors="surrogateescape")
    matched: dict | None = None
    with _lock:
        for entry in _keys:
            entry_bytes = entry["key"].encode("utf-8", errors="surrogateescape")
            if hmac.compare_digest(candidate_bytes, entry_bytes):
                matched = dict(entry)
    if matched is None:
        return AUTH_NOT_FOUND, None
    if not matched["enabled"]:
        return AUTH_DISABLED, None
    return AUTH_VALID, matched
