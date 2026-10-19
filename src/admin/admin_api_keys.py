# -*- coding: utf-8 -*-
"""
API Key 管理端点
================
提供 /api/admin/api-keys 的增删改查，供用户在首次使用（尚无任何 key）时
经管理界面自助创建 /v1/* 的鉴权凭据。

安全约束：
- 本蓝图挂在 admin_ui 下，继承父蓝图的 before_request（口令会话门禁），
  不要重复挂载鉴权，也不要把本蓝图提升为 app 级独立蓝图
- 明文只写 api-keys.json（0600），日志与异常不得输出 key 值
"""
import logging

from flask import Blueprint, Response, jsonify, request

from src.auth import api_keys
from src.config import config_store

logger = logging.getLogger(__name__)

blueprint = Blueprint("admin_api_keys", __name__)


def _json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def _valid_key_name(value: object) -> str | None:
    """校验名称：去首尾空白后非空、≤NAME_MAX_LEN 字符、可打印（换行已被覆盖）；非法返回 None。"""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > api_keys.NAME_MAX_LEN:
        return None
    if not name.isprintable():
        return None
    return name


@blueprint.route("/api/admin/api-keys", methods=["GET"])
def admin_list_api_keys():
    """列出全部 key（含明文，管理面自身已由口令会话保护）与强制鉴权开关。"""
    switch = config_store.get("CODEARTS_REQUIRE_API_KEY", "1").strip()
    return jsonify({
        "keys": api_keys.list_keys(),
        "require_api_key": "0" if switch == "0" else "1",
    })


@blueprint.route("/api/admin/api-keys", methods=["POST"])
def admin_create_api_key():
    """新建 key；响应是明文唯一一次显式下发（列表接口同样可读）。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("请求体必须为 JSON 对象", 400)
    name = _valid_key_name(data.get("name"))
    if name is None:
        return _json_error(
            f"名称必须为 1–{api_keys.NAME_MAX_LEN} 字符的单行可打印文本", 400
        )
    try:
        entry = api_keys.create_key(name)
    except OSError as error:
        logger.error("保存 API Key 失败: %s", error)
        return _json_error("保存 API Key 失败，请检查 api-keys.json 所在目录权限", 500)
    return jsonify({"key": entry}), 201


@blueprint.route("/api/admin/api-keys/<int:key_id>", methods=["PATCH"])
def admin_update_api_key(key_id: int):
    """改名 / 启停；未提供的字段保持不变。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _json_error("请求体必须为 JSON 对象", 400)
    if "name" not in data and "enabled" not in data:
        return _json_error("至少提供 name 或 enabled 之一", 400)
    name = None
    if "name" in data:
        name = _valid_key_name(data.get("name"))
        if name is None:
            return _json_error(
                f"名称必须为 1–{api_keys.NAME_MAX_LEN} 字符的单行可打印文本", 400
            )
    enabled = None
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            return _json_error("enabled 必须为布尔值", 400)
        enabled = data["enabled"]
    try:
        entry = api_keys.update_key(key_id, name=name, enabled=enabled)
    except OSError as error:
        logger.error("保存 API Key 失败: %s", error)
        return _json_error("保存 API Key 失败，请检查 api-keys.json 所在目录权限", 500)
    if entry is None:
        return _json_error("API Key 不存在", 404)
    return jsonify({"key": entry})


@blueprint.route("/api/admin/api-keys/<int:key_id>", methods=["DELETE"])
def admin_delete_api_key(key_id: int):
    """删除 key（立即失效）。"""
    try:
        deleted = api_keys.delete_key(key_id)
    except OSError as error:
        logger.error("保存 API Key 失败: %s", error)
        return _json_error("保存 API Key 失败，请检查 api-keys.json 所在目录权限", 500)
    if not deleted:
        return _json_error("API Key 不存在", 404)
    return jsonify({"ok": True})
