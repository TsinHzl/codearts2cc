# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

把华为云 CodeArts 模型接口包装成标准 OpenAI Chat Completions API 的本地 Flask 代理。上游认证走华为云 `SDK-HMAC-SHA256` 请求签名，凭据来自 CodeArts OAuth 登录（管理后台「登录」页签浏览器授权；refresh_token + DPoP 私钥落盘 `oauth-auth.json`（0600，gitignore），STS 临时 AK/SK 自动签发与续期，未登录时聊天端点返回 503 信封）。代理自身默认对 `/v1/*` 强制本地 API Key 鉴权（`x-api-key` 优先，`Authorization: Bearer` 次之，`CODEARTS_REQUIRE_API_KEY=0` 关闭；未设置 `ADMIN_PASSWORD` 时管理面 API 禁用（403）、鉴权放行），key 存于 `api-keys.json`（0600，gitignore），由管理后台「API Key」页签自助生成；默认仅绑定 127.0.0.1。

## 常用命令

```bash
pip install -r requirements.txt   # 依赖：flask / requests
python -m src.server              # 启动代理，监听 http://127.0.0.1:51818（可用 CODEARTS_PROXY_HOST / CODEARTS_PROXY_PORT 覆盖）
python tests/test_openai.py       # 集成冒烟测试（需先启动 server 并已生成 API Key；缺省读 api-keys.json 首个启用 key，可用 --key 覆盖）
python -m pytest                  # 单元测试（需 pip install -r requirements-dev.txt）
python -m src.tools.benefit       # 签到 + 查询免费额度余额
python -m src.tools.benefit status  # 只查签到状态（其余子命令：claim / models）
```

无 lint 配置；单测在 `tests/`（pytest，`tests/test_anthropic_proxy.py` / `tests/test_cache_sim.py` / `tests/test_admin_ui.py` 等），`tests/test_openai.py` 是集成冒烟脚本。

## 架构

采用 src 标准包结构：`src/server.py`（主服务：签名 + 转发 + `/v1/*` API Key 门禁 + OAuth 回调路由）、`src/proxy/anthropic_proxy.py`（Anthropic Messages 兼容端点）、`src/proxy/cache_sim.py`（prompt cache usage 模拟）、`src/oauth/oauth_login.py`（CodeArts OAuth 凭据管理：STS 快照 / DPoP / 刷新 / 退避）、`src/oauth/oauth_protocol.py`（OAuth 协议常量与 PKCE / DPoP 工具）、`src/oauth/oauth_sessions.py`（登录会话仓库：双阶段回调匹配）、`src/admin/admin_oauth.py`（管理后台登录页签蓝图）、`src/admin/admin_ui.py`（模型/额度/配置管理页面；前端为 React 19 + TypeScript + Tailwind CSS 4，源码在 `admin-ui/`，Vite 预构建产物 `admin-ui/dist/` 入库，改前端需 `npm run build` 重新产出，运行 server 零 Node 依赖）、`src/auth/api_keys.py`（API Key 存储与恒定时间校验）、`src/admin/admin_api_keys.py`（管理后台 API Key 蓝图）、`src/usage/usage_log.py`（使用日志存储与聚合）、`src/usage/pricing.py`（模型单价表与费用 / Credits 估算）与 `src/tools/benefit.py`（每日免费额度签到工具，独立运行，与 server 共享 `config.json`）。`src/paths.py` 统一定位项目根与运行时文件路径，`src/config/config_store.py` 负责配置读写；测试统一位于 `tests/`。

### API Key 鉴权（api_keys.py / admin_api_keys.py）

- **生效范围**：app 级 `before_request` 只拦 `/v1/*` 且跳过 `OPTIONS`；`x-api-key` 优先、`Authorization: Bearer` 次之；缺 key / 无效 key → 401（key 列表为空时消息提示先创建），已禁用 → 403。错误体按 `/v1/messages*` 与 OpenAI 路径分流。
- **总开关**：`CODEARTS_REQUIRE_API_KEY`（默认 `1`）为 `0` 时整体跳过；未设置 `ADMIN_PASSWORD`（管理面禁用、无 key 生成入口）时也自动放行——即鉴权仅在设置 `ADMIN_PASSWORD` 后真正生效。
- **存储**：`api-keys.json`（`[{id, key, name, enabled, createdAt}]`，按 id 升序），原子写（先 `os.open` 0600 建 tmp 再 `os.replace`），模块内锁；写盘失败回滚内存态。key 明文只存该文件，**不进日志与异常**。
- **id 约束**：当前 `max(id)+1`；引入按 key 用量统计前必须先加独立 id 计数器，否则删除后 id 复用会继承历史用量。
- **管理面**：`admin_api_keys.py` 为嵌套蓝图，继承 `admin_ui` 的 `before_request`（口令会话），不要重复挂载。

### 使用日志与计价（pricing.py）

`usage_log.py` 以 JSONL 落盘 + 内存聚合，`query()` 支持 `start` / `end` / `daily`；`_entries` 保留全量，超过 10000 条时滚动保留较新一半。`pricing.py` 按合并单价表（`{"<上游模型 id>": {"input", "output", "cache_read", "cache_creation"}}`，USD / 1M tokens）计算费用、Credits（× `CODEARTS_CREDITS_RATE`，默认 1.0）与缓存节省额；单价表由官方单价表与 `CODEARTS_PRICING` 合并而成——配置优先，未覆盖的模型回退官方价（模型刷新时从 `GET {snap}/v1/model/builtin` 拉取 `credit` 档位写入 `models-cache.json` 的 `credit_pricing`，取 `time_slot_id` 为 null/`default` 的首档，忽略 `ratio`）。两者均未覆盖的模型返回 `None`，界面显示「–」。计价为本地估算口径。`CODEARTS_ACCOUNT_NAME` 控制使用日志「账号」列（默认「主账号」）。使用日志「思考强度」列取请求的 effort：优先级 `output_config.effort` → `thinking.effort`，均无时补 `high`（与发给上游的 `reasoning_effort` 同源同口径），`thinking.type=="disabled"` 时为 `null`（列内显示「–」）。计价为本地估算，`cache_read_tokens` 仅 Anthropic 端点由 `cache_sim.py` 模拟。使用日志「思考」列与明细行「思考 Tokens」取本次响应的思考 token 数：优先上游 `usage.completion_tokens_details.reasoning_tokens`（int、非 bool 且 ≥0），否则按思考文本字符数 // 4 估算（Anthropic 端点由 `anthropic_proxy.py` 流式累积，含被限额丢弃的段、不受 `thinking_as_text` 开关影响）；两来源统一截断 ≤ `output_tokens`，双来源皆无时省略键（明细显示「–」，聚合按 0 计）。该值为 `output_tokens` 子集，不参与计价，汇总「本页 Token」亦不计入以免双重计数；÷4 为英文近似，中文思考文本系统性偏低估，估算值仅作量级观测。

### Anthropic Messages 端点（/v1/messages）

`anthropic_proxy.py` 把 Anthropic Messages 协议转成 OpenAI Chat Completions 后走同一条 CodeArts 上游链路，供 Claude Code 通过 `ANTHROPIC_BASE_URL` 直连。镜像 `../kiro2cc-proxy/`（方向相反）的结构：

- **模型映射三步法**（`resolve_model`）：剥 `[4m]` 1M 上下文后缀 → 查 `CODEARTS_ANTHROPIC_MODEL_MAP` 别名表（config.json 覆盖默认表）→ `claude-*` 未命中兜底 `SUPPORTED_MODELS[0]`+警告。默认表：`claude-sonnet-4-5 → openpangu-2.0-pro`、`claude-haiku-4-5 → glm-5.3-flash`（免费 benefit）。
- **依赖注入（关键约束）**：`python -m src.server` 启动时主模块是 `__main__`，`anthropic_proxy` 延迟 `import server` 会产生第二个模块实例（全局停留兜底列表 → agent 模型误判 benefit）。因此 `src/server.py` 末尾 `configure()` 显式注入：`AGENT_MODELS` 传引用、`SUPPORTED_MODELS`/`MODEL_DETAILS` 传 getter（模型刷新时被重绑定）、`_hmac_headers`/`_parse_upstream_json` 传函数引用。**禁止任何形式的延迟 import server**。
- **tool_result 转换**：每个 tool_result 块 → 独立 `role:"tool"` 消息；user turn 内混合块按「tool 先、text 后」重排。
- **层间契约**：怪癖修补层（`patch_stream_to_chunks`，输入上游原始字节流）输出「归一化 OpenAI chunk 序列」（finish_reason 必有、content 非空串），`AnthropicStreamConverter` 状态机假设输入合规；业务错误（HTTP 200 + SSE 帧 error_code）在修补层抛 ValueError → 流式路径转 Anthropic error 事件。
- **stop_reason 映射**：stop→end_turn、tool_calls→tool_use、length→max_tokens、content_filter→refusal、未知→end_turn+警告。
- **思考文本化**（`CODEARTS_THINKING_AS_TEXT`，默认开启）：UA 匹配 `^(?:claude-cli|claude-code)(?:/|\s|$)` 时，`reasoning_content` 改写为灰色文本而非原生 thinking 块。流式由 `AnthropicStreamConverter(thinking_as_text=True)` 惰性打开 text 块、逐完整行输出（`\n\x1b[2m<line>\x1b[0m`），段末补 `💭 Thought for Xs` 与尾随换行；非流式由 `openai_completion_to_anthropic` 输出独立 text 块（无时长行、不限额）。限额：单段 256 KiB / 响应 1 MiB。路由层在转换与缓存估算前用 `strip_thinking_text_from_messages` 剥离历史思考文本，该剥离仅由 UA 决定、不受开关控制。
- tools 已实测走 OpenAI `tools` 格式上游可接受（原「待验证假设」已验证）。

### OAuth 凭据体系（oauth_login.py / oauth_protocol.py / oauth_sessions.py）

- **凭据生命周期**：管理页发起登录（PKCE + DPoP）→ 浏览器授权 → `/oauth/callback` 双阶段回调（secret / code）→ token 交换 → 快照（含 STS 临时 AK/SK）原子写 `oauth-auth.json`；到期前 30 分钟内自动刷新，`invalid_grant` 置 needs_relogin 且退避不再请求。
- **签名注入**：聊天 / 模型发现 / 签到统一取 STS 快照签名（附加 `X-Security-Token` 与 `X-Sdk-Content-Sha256`，均参与 `SignedHeaders`）；DPoP 私钥仅本地使用。
- **关键约束**：凭据明文（refresh_token、DPoP 私钥、STS AK/SK、security_token）不得进日志 / 异常 / 响应；`src/server.py` 末尾 `configure()` 注入 `oauth_status` 回调；`anthropic_proxy` 禁止延迟 `import server`（见下文依赖注入）。
- **管理面门禁**：未配置 `ADMIN_PASSWORD` 时 `/admin` 壳与静态资源放行（200 无数据）、仅 `/api/admin*` 403；已配置时壳放行、API 未登录 401。

### 双通道模型体系（最关键的设计）

模型分两条上游通道，签名方式不同，**严格互斥**。聊天补全统一 POST 到 `snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions`；opengw 网关仅用于免费模型列表发现与签到：

| 通道 | 模型例 | 签名差异 |
|---|---|---|
| `agent`（老套餐） | `openpangu-2.0-pro`、`GLM-5.2`、`Qwen3-VL-235B` | 基础签名（host / content-type / x-sdk-date） |
| `benefit`（免费额度） | `glm-5.3-flash`、`deepseek-v4-*` | 额外附加 `maas_type: benefit` + `model-id` + `model-name` 三个头并参与签名 |

- benefit 模型缺这三个头 → 上游报 "model is not registered"；agent 模型带上 → 报 "unsupported model"。
- 通道判断：`model not in AGENT_MODELS` 即按 benefit 处理（`chat_completions` 内），云端新增免费模型无需改代码。
- **图片输入（多模态）**：Anthropic 端点 `image` 块转 OpenAI `image_url`（base64 → data URI，`source.type=="url"` 直传 URL）；上游聊天端点已实测接受该格式（非多模态模型报 406 "not multimodal"）。视觉模型当前仅 `Qwen3-VL-235B`（agent 通道；AgentCenter 发现接口不返回，由 `EXTRA_AGENT_MODELS` 本地维护并标记 `supports_images: true`，经 `_merge_extra_agent_models` 并入云端发现与缓存加载两条路径，云端返回同名模型后以云端为准）；其余模型带图请求在路由层 400 拒绝。Claude Code 要用图需配 `CODEARTS_ANTHROPIC_MODEL_MAP=claude-sonnet-4-5:Qwen3-VL-235B`（默认映射仍指向 openpangu-2.0-pro，不支持图片）。
- `AGENT_MODELS` 由模型详情的 `channel` 字段驱动，来自缓存或云端同步。

### 两套签名实现

- `_signed_request(method, url, ...)`：通用版，用于模型发现类 GET（AgentCenter、opengw），需 `agent-type: AgentCenter` 等额外路由头。
- `_hmac_headers(body, benefit, model_id)`：专用于聊天补全端点，canonical URI 硬编码且**带尾斜杠**（`/api/v2/chat/completions/`）。

两处均实现 SDK-HMAC-SHA256（canonical request → string to sign → HMAC-SHA256）；参与签名的头名必须转小写后排序。

### 模型列表生命周期

启动 `_refresh_models()` → 原子写入 `models-cache.json`（tmp + `os.replace`）→ 请求时 `_refresh_models_if_needed()` 按 300 秒 TTL 刷新。回退链：云端同步 → 缓存文件 → 硬编码兜底列表（`FALLBACK_AGENT_MODELS` + `BENEFIT_MODELS`）。新增模型需先抓包确认通道再更新兜底列表。

### 上游 SSE 的已知怪癖（改动流式逻辑前必读）

- 请求 `stream=false` 上游仍可能返回 `text/event-stream`；非流式路径统一走 `_parse_upstream_json` 聚合（内部 `_parse_sse_bytes`）。
- 业务错误可能以 HTTP 200 + SSE 帧内 `error_code`/`error_msg` 形式返回，需转成 502。
- benefit 流可能完全没有 `finish_reason` → 代理在 `[DONE]` 前合成一个 `finish_reason: "stop"` 的补块（`id: "proxy-finish"`），`[DONE]` 因此被延迟发送；agent 流保持原样即时转发。
- benefit 流（DeepSeek）可能发 `content: null` + `reasoning_content` → 改写为 `content: ""`，否则部分 Agent Runtime 误判为空响应。
- `finish_reason: "other"` 一律归一化为 `"stop"`（`_normalize_finish_reason`）。
