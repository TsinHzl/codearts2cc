# -*- coding: utf-8 -*-
"""本地测试脚本：通过代理调用 CodeArts 模型。

代理的 /v1/* 默认强制 API Key 鉴权：本脚本缺省读取仓库根目录 api-keys.json
中首个启用的 key，也可用 --key 显式传入。首次使用请先在管理后台
http://127.0.0.1:51818/admin 的「API Key」页签生成，并在「登录」页签
完成 CodeArts OAuth 登录（未登录时上游调用会失败）。
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = "http://127.0.0.1:51818/v1"
API_KEYS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "api-keys.json"
)


def read_default_key() -> str:
    """取 api-keys.json 中首个启用的 key；文件缺失、损坏或无可用 key 时返回空串。"""
    try:
        with open(API_KEYS_PATH, "r", encoding="utf-8") as handle:
            entries = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        key = entry.get("key")
        if isinstance(key, str) and key:
            return key
    return ""


def check_models(headers: dict) -> None:
    req = urllib.request.Request(BASE + "/models", headers=headers)
    with urllib.request.urlopen(req) as r:
        print("== GET /models ==")
        print(json.dumps(json.loads(r.read()), ensure_ascii=False, indent=2))


def check_chat(headers: dict) -> None:
    payload = {
        "model": "openpangu-2.0-pro",
        "messages": [{"role": "user", "content": "只回复：成功"}],
        "stream": False,
        "max_tokens": 64,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        print("\n== POST /chat/completions ==")
        data = json.loads(r.read())
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])


def main() -> None:
    parser = argparse.ArgumentParser(description="通过本地代理调用 CodeArts 模型")
    parser.add_argument("--key", help="API Key；缺省读取 api-keys.json 中首个启用的 key")
    args = parser.parse_args()
    key = args.key or read_default_key()
    if not key:
        sys.exit(
            "未找到可用的 API Key：请先在 http://127.0.0.1:51818/admin 的「API Key」页签生成，"
            "或用 --key 显式传入"
        )
    headers = {"x-api-key": key}
    check_models(headers)
    check_chat(headers)


if __name__ == "__main__":
    main()
