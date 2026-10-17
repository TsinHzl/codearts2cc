# config_store.py
# -*- coding: utf-8 -*-
"""集中配置模块：config.json 的唯一读写入口。

取代 .env + python-dotenv：server / benefit / anthropic_proxy / cache_sim /
admin_ui 全部经此读取配置；admin 配置页热更新走 set_many（原子写回）。
文件不存在视为空配置（各键走代码内默认值）；JSON 损坏直接拒绝启动。
"""
import json
import os
import shutil
import threading

_path = ""          # config.json 绝对路径（init 注入）
_data = {}          # 键 → 值（值统一按字符串读出）
_lock = threading.Lock()  # set_many 读-改-写互斥（Flask threaded=True）


def init(path: str) -> None:
    """加载 config.json 到内存；文件不存在视为空配置，损坏则拒绝启动。"""
    global _path, _data
    _path = path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            _data = json.load(handle)
    except FileNotFoundError:
        _data = {}
    except json.JSONDecodeError as error:
        raise SystemExit(f"config.json 解析失败: {error}")
    if not isinstance(_data, dict):
        raise SystemExit("config.json 解析失败: 顶层必须是 JSON 对象")
    if os.path.exists(path):
        os.chmod(path, 0o600)  # 含 AK/SK/口令明文，兜底收紧权限


def get(key: str, default: str = "") -> str:
    """读取配置值；统一归一为 str（容忍 JSON 中写成数字的端口等）。"""
    value = _data.get(key, default)
    return default if value is None else str(value)


def set_many(mapping: dict) -> None:
    """批量更新并原子写回（tmp + os.replace，保留原文件权限）。"""
    global _data
    with _lock:
        _data = dict(_data)
        _data.update(mapping)
        tmp_path = _path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(_data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if os.path.exists(_path):
            shutil.copymode(_path, tmp_path)
        else:
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, _path)


def as_dict() -> dict:
    """返回全量配置浅拷贝（admin 配置页读取）。"""
    return dict(_data)
