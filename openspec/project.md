# 项目上下文

## 技术栈
Python 3 + Flask（无数据库）；requests 做上游 HTTP。CodeArts 凭据经管理后台「登录」页签 OAuth 登录获取（PKCE + DPoP），STS 临时 AK/SK 落盘 `oauth-auth.json`（0600）并自动续期。

## 架构约定
单文件代理（server.py）：OpenAI Chat Completions 入口 → 华为云 SDK-HMAC-SHA256 签名（STS 临时凭据 + X-Security-Token / X-Sdk-Content-Sha256）→ 转发 CodeArts 上游（snap-access）。模型分 agent（老套餐）/ benefit（免费额度）双通道，签名头严格互斥；通道由模型详情 `channel` 字段驱动。上游 SSE 怪癖（finish_reason 缺失、content:null、finish_reason:"other"）在代理层修正，详见 CLAUDE.md。

## 目录结构
- `server.py` — 主服务：签名 + 转发 + 模型列表生命周期（缓存 models-cache.json，300s TTL）
- `benefit.py` — 免费额度签到/余额/模型查询 CLI
- `test_openai.py` — 对运行中服务的集成冒烟脚本（无单测框架；缺省读 api-keys.json 首个启用 key，可 `--key` 覆盖）
- `api_keys.py` — API Key 存储与校验（api-keys.json，0600，原子写）
- `admin_api_keys.py` — 管理后台 API Key 路由（嵌套蓝图，继承 admin_ui 口令鉴权）
- `oauth_login.py` — OAuth 凭据管理（STS 快照 / DPoP / 刷新 / 退避）
- `oauth_protocol.py` — OAuth 协议常量与 PKCE / DPoP 工具
- `oauth_sessions.py` — OAuth 登录会话仓库（双阶段回调匹配）
- `oauth-auth.json` — OAuth 凭据落盘（运行时生成，0600，不入库）
- `models-cache.json` — 模型缓存（原子写）

## 开发约定
- 中文注释与用户可见错误消息；类型注解；PEP 8
- 单测 pytest（test_*.py，需 requirements-dev.txt）；`python test_openai.py` 为集成冒烟（需先启动 server、完成「登录」页签 OAuth 登录并生成 API Key）
- 约束：OAuth 凭据明文（refresh_token / DPoP 私钥 / STS AK/SK / security_token）只落盘 0600 文件，不进日志 / 异常 / 文档；默认仅绑定 127.0.0.1；`/v1/*` 默认强制本地 API Key 鉴权，仅在设置 `ADMIN_PASSWORD` 且 `CODEARTS_REQUIRE_API_KEY` 非 `0` 时真正拦截（未设口令则管理面 API 禁用、无 key 生成入口，鉴权同步放行）；不修改模型行为只做协议适配；对外网暴露需自备 TLS 与网络边界
