# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

把华为云 CodeArts 模型接口包装成标准 OpenAI Chat Completions API 的本地 Flask 代理。认证走华为云 `SDK-HMAC-SHA256` 请求签名，AK/SK 在 `.env`（`CODEARTS_AK` / `CODEARTS_SK`，缺失时启动直接退出）。代理本身不鉴权（API Key 任意非空即可），仅绑定 127.0.0.1。

## 常用命令

```bash
pip install -r requirements.txt   # 依赖：flask / requests / python-dotenv
python server.py                  # 启动代理，监听 http://127.0.0.1:8787（可用 CODEARTS_PROXY_HOST / CODEARTS_PROXY_PORT 覆盖）
python test_openai.py             # 集成冒烟测试（需先启动 server；纯标准库，对运行中的服务打真实请求）
python -m pytest test_anthropic_proxy.py   # Anthropic 协议转换单元测试（需 pip install -r requirements-dev.txt）
python benefit.py                 # 签到 + 查询免费额度余额
python benefit.py status          # 只查签到状态（其余子命令：claim / models）
```

无 lint 配置；单测只有 `test_anthropic_proxy.py`（pytest），`test_openai.py` 是集成冒烟脚本。

## 架构

逻辑集中在 `server.py`（主服务：签名 + 转发）、`anthropic_proxy.py`（Anthropic Messages 兼容端点）与 `benefit.py`（每日免费额度签到工具，独立运行，与 server 共享 `.env`）。无包结构。

### Anthropic Messages 端点（/v1/messages）

`anthropic_proxy.py` 把 Anthropic Messages 协议转成 OpenAI Chat Completions 后走同一条 CodeArts 上游链路，供 Claude Code 通过 `ANTHROPIC_BASE_URL` 直连。镜像 `../kiro2cc-proxy/`（方向相反）的结构：

- **模型映射三步法**（`resolve_model`）：剥 `[4m]` 1M 上下文后缀 → 查 `CODEARTS_ANTHROPIC_MODEL_MAP` 别名表（env 覆盖默认表）→ `claude-*` 未命中兜底 `SUPPORTED_MODELS[0]`+警告。默认表：`claude-sonnet-4-5 → openpangu-2.0-pro`、`claude-haiku-4-5 → glm-5.3-flash`（免费 benefit）。
- **依赖注入（关键约束）**：`python server.py` 启动时主模块是 `__main__`，`anthropic_proxy` 延迟 `import server` 会产生第二个模块实例（全局停留兜底列表 → agent 模型误判 benefit）。因此 `server.py` 末尾 `configure()` 显式注入：`AGENT_MODELS` 传引用、`SUPPORTED_MODELS`/`MODEL_DETAILS` 传 getter（模型刷新时被重绑定）、`_hmac_headers`/`_parse_upstream_json` 传函数引用。**禁止任何形式的延迟 import server**。
- **tool_result 转换**：每个 tool_result 块 → 独立 `role:"tool"` 消息；user turn 内混合块按「tool 先、text 后」重排。
- **层间契约**：怪癖修补层（`patch_stream_to_chunks`，输入上游原始字节流）输出「归一化 OpenAI chunk 序列」（finish_reason 必有、content 非空串），`AnthropicStreamConverter` 状态机假设输入合规；业务错误（HTTP 200 + SSE 帧 error_code）在修补层抛 ValueError → 流式路径转 Anthropic error 事件。
- **stop_reason 映射**：stop→end_turn、tool_calls→tool_use、length→max_tokens、content_filter→refusal、未知→end_turn+警告。
- tools 已实测走 OpenAI `tools` 格式上游可接受（原「待验证假设」已验证）。

### 双通道模型体系（最关键的设计）

模型分两条上游通道，签名方式不同，**严格互斥**。聊天补全统一 POST 到 `snap-access.cn-north-4.myhuaweicloud.com/api/v2/chat/completions`；opengw 网关仅用于免费模型列表发现与签到：

| 通道 | 模型例 | 签名差异 |
|---|---|---|
| `agent`（老套餐） | `openpangu-2.0-pro`、`GLM-5.2`、`Qwen3-VL-235B` | 基础签名（host / content-type / x-sdk-date） |
| `benefit`（免费额度） | `glm-5.3-flash`、`deepseek-v4-*` | 额外附加 `maas_type: benefit` + `model-id` + `model-name` 三个头并参与签名 |

- benefit 模型缺这三个头 → 上游报 "model is not registered"；agent 模型带上 → 报 "unsupported model"。
- 通道判断：`model not in AGENT_MODELS` 即按 benefit 处理（`chat_completions` 内），云端新增免费模型无需改代码。
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
