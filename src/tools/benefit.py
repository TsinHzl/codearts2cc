# -*- coding: utf-8 -*-
"""
CodeArts 免费额度签到/查询工具
==============================
对应 IDE 里的免费模型福利机制（opengw.developer.huaweicloud.com，抓包确认）：

    GET  /api/v1/benefit/claim        签到状态（返回上次签到时间）
    POST /api/v1/benefit/claim        签到领取当日免费额度（空 body）
    GET  /api/v1/user/tokens/balance  查询免费额度余额
    GET  /api/v1/gateway/config       免费模型列表

用法（需先在管理页完成 OAuth 登录，凭据由 oauth-auth.json 提供）：

    python -m src.tools.benefit            # 签到 + 查询余额
    python -m src.tools.benefit status     # 只查询，不签到
    python -m src.tools.benefit claim      # 只签到
    python -m src.tools.benefit models     # 查询免费模型列表

配合 Windows 计划任务可实现每日自动签到，例如每天 09:05 执行（需在仓库根目录）：
    schtasks /create /tn CodeArtsBenefit /tr "cmd /c cd /d C:\\...\\codearts2cc && python -m src.tools.benefit claim" /sc daily /st 09:05
"""
import datetime
import sys

import requests

from src import paths
from src.oauth import oauth_login

OAUTH_CRED_PATH = paths.OAUTH_CRED_PATH

OPENGW_BASE = "https://opengw.developer.huaweicloud.com"
CLAIM_URL = OPENGW_BASE + "/api/v1/benefit/claim"
BALANCE_URL = OPENGW_BASE + "/api/v1/user/tokens/balance"
CONFIG_URL = OPENGW_BASE + "/api/v1/gateway/config"


def signed_request(method: str, url: str, body: bytes = b"") -> requests.Response:
    """发送带华为云 SDK-HMAC-SHA256 签名的请求（签名头由 oauth_login 统一构造）。"""
    headers = oauth_login.sign_headers(method, url, body)
    return requests.request(
        method.upper(), url, data=body or None, headers=headers, timeout=30
    )


def _unwrap(response: requests.Response) -> dict:
    """校验 opengw 响应（error_code=0000 表示成功），返回 result 字段。"""
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except ValueError as error:
        # 网关异常页/空响应等非 JSON 载荷：统一转 RuntimeError 供 main 捕获
        raise RuntimeError(f"响应非 JSON: {response.text[:300]}") from error
    if data.get("error_code") not in (None, "0000", 0):
        raise RuntimeError(f"业务错误 {data.get('error_code')}: {data.get('error_msg')}")
    return data.get("result") or {}


def claim_status() -> dict:
    """查询签到状态。create_time 即最近一次签到时间（毫秒时间戳）。"""
    return _unwrap(signed_request("GET", CLAIM_URL))


def claim() -> dict:
    """签到领取当日免费额度。"""
    return _unwrap(signed_request("POST", CLAIM_URL))


def balance() -> dict:
    """查询免费额度余额。"""
    return _unwrap(signed_request("GET", BALANCE_URL))


def models() -> list:
    """查询免费模型列表。"""
    return (_unwrap(signed_request("GET", CONFIG_URL)) or {}).get("models") or []


def _fmt_time(ms) -> str:
    if not ms:
        return "从未签到"
    local = datetime.datetime.fromtimestamp(ms / 1000)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def show_balance() -> None:
    info = balance()
    total_quota = info.get("total_quota") or 0
    remain = info.get("total_balance") or 0
    used = info.get("used_amount") or 0
    percent = (remain / total_quota * 100) if total_quota else 0
    print(f"免费额度余额: {remain:,} / {total_quota:,}（已用 {used:,}，剩余 {percent:.1f}%）")


def show_status() -> None:
    info = claim_status()
    print(f"最近签到时间: {_fmt_time(info.get('create_time'))}")


def do_claim() -> None:
    info = claim()
    print(f"签到成功，最近签到时间: {_fmt_time(info.get('create_time'))}")


def show_models() -> None:
    for model in models():
        ctx = model.get("context_window")
        out = model.get("max_tokens")
        # 两个数值字段都到位才走格式化分支，避免 None 触发 TypeError
        print(f"  {model.get('model_id')}  (上下文 {ctx:,} / 最大输出 {out:,})" if ctx and out else
              f"  {model.get('model_id')}")


def main(argv: list) -> int:
    oauth_login.init(OAUTH_CRED_PATH)  # CLI 独立运行；server 导入时已由 server.py init
    action = argv[1] if len(argv) > 1 else "default"
    try:
        if action == "status":
            show_status()
        elif action == "claim":
            do_claim()
            show_balance()
        elif action == "models":
            print("免费模型列表:")
            show_models()
        else:
            # 默认：签到 + 状态 + 余额
            show_status()
            do_claim()
            show_balance()
    except oauth_login.CredentialsUnavailable:
        print("失败: 尚未登录 CodeArts，请先运行 server.py 并在管理页（/admin）完成登录")
        return 1
    except (RuntimeError, requests.RequestException, ValueError) as error:
        print(f"失败: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
