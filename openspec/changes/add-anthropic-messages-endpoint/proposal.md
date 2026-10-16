# 变更提案：add-anthropic-messages-endpoint

## 背景

当前 `server.py` 只暴露 OpenAI 协议端点（`/v1/chat/completions`、`/v1/models`）。Claude Code 客户端只讲 Anthropic Messages 协议（`ANTHROPIC_BASE_URL` → `POST /v1/messages`，SSE 事件为 `message_start / content_block_delta / message_delta / message_stop`），无法直接把本代理作为其模型后端。

参考实现：`../kiro2cc-proxy/src/openai/`（方向相反——OpenAI 入口转 Anthropic 出口），本变更为其镜像：**Anthropic Messages 入口 → 转成 OpenAI Chat Completions → 复用现有 CodeArts 签名链路与上游怪癖处理经验**。

## 目标范围

**在范围内：**
- 新增 `POST /v1/messages`（Anthropic Messages 协议，支持流式/非流式）
- 模型名映射（三步法）：1) 剥离 Claude Code 的 `[4m]` 1M 上下文后缀；2) 查 `CODEARTS_ANTHROPIC_MODEL_MAP` 别名表（`alias:model` 逗号分隔，env 可覆盖默认表）；3) `claude-*` 未命中兜底 `SUPPORTED_MODELS[0]` 并打印警告，非 `claude-*` 名称走本端点自行实现的校验（不在 `SUPPORTED_MODELS` → Anthropic 格式 404）
- 内置默认映射表：`claude-sonnet-4-5 → openpangu-2.0-pro`（主工作模型）、`claude-haiku-4-5 → glm-5.3-flash`（免费 benefit，后台小任务不耗套餐额度）
- 请求翻译：system（string / blocks 两种形态）→ OpenAI system 消息；content blocks（text / tool_use / tool_result / image base64）→ OpenAI messages；tools → OpenAI `tools` 格式（**待验证假设**，若上游只认 `functions` 按验证结果调整）；`max_tokens`/`temperature`/`top_p`/`stop_sequences` 透传
- tool_result 转换规则：每个 `tool_result` 块 → **独立 `role:"tool"` OpenAI 消息**（带 `tool_call_id`，逐条独立不合并）；user turn 内 tool_result 与 text 混合时按 **「tool 先、text 后」** 重排（text 合并为一条 user 消息排在全部 tool 消息之后，满足 OpenAI 的 tool 消息紧随 assistant tool_calls 的约束）
- 响应翻译（非流式）：OpenAI completion → Anthropic message（`id`/`content` blocks/`stop_reason`/`usage`），`reasoning_content` → `thinking` block
- 怪癖修补层（`anthropic_proxy.py` 自行实现，不改 `chat_completions`）：补 finish_reason、content:null 归空串、finish_reason:"other" 归一化、benefit 流缺 finish_reason 时补块、**stream=true 上游返回普通 JSON 时先聚合为单条 completion 再进状态机**、流式路径 HTTP 200 + SSE 帧内 error_code/error_msg → Anthropic error 事件
- 响应翻译（流式）：修补后的 OpenAI chunk 流 → `AnthropicStreamConverter` 状态机 → Anthropic SSE 事件序列；**usage 位置**：`message_start` 带 `usage.input_tokens`，`message_delta` 带累计 `output_tokens`；thinking/text/tool_use 三类块切换时输出 `content_block_stop`/`content_block_start` 对；tool_calls 增量 arguments → `input_json_delta`
- `stop_reason` 映射：`stop`→`end_turn`、`tool_calls`→`tool_use`、`length`→`max_tokens`、`content_filter`→`refusal`、未知值→`end_turn`+警告
- `/v1/messages` 错误响应采用 Anthropic 格式（`{"type":"error","error":{"type","message"}}`）
- `supports_images=false` 的模型收到 image block → Anthropic 格式 400 `invalid_request_error`，不发上游

**不在范围内：**
- `/v1/messages/count_tokens`、prompt caching（`cache_control` 忽略即可）
- thinking 块签名回传等上游无法支撑的协议细节；`thinking` 请求参数（`budget_tokens`）透传
- WebSearch（`web_search_20250305`）等 server-side 工具
- `server.py` 的 `chat_completions` 及其内部闭包的任何改动
- `/v1/models` 格式调整（Claude Code 不依赖，保持 OpenAI 格式原样）
- 多账号、限流、用量统计

## 技术方案

- 新增独立模块 `anthropic_proxy.py`：协议转换纯函数 + 流式状态机 + Flask 蓝图；`server.py` 末尾仅追加蓝图注册与依赖注入代码
- **依赖注入（禁止延迟 `import server`）**：`python server.py` 启动时主模块是 `__main__`，惰性 `import server` 会把 server.py 作为第二个模块实例重新执行，其全局状态永远停留在兜底列表（模型刷新只更新 `__main__` 的全局），导致 agent 通道模型被误判为 benefit。因此 `server.py` 注册蓝图时显式传入：
  - `AGENT_MODELS`：原地 `.clear()/.extend()` 变更，传列表引用
  - `SUPPORTED_MODELS` / `MODEL_DETAILS`：模型刷新时被重绑定，传 getter（如 `lambda: SUPPORTED_MODELS`，闭包捕获当前模块命名空间）
  - `_hmac_headers` / `_parse_upstream_json`：不重绑定，直接传函数引用
- 通道判断与模型校验由蓝图路由自行实现：`model not in SUPPORTED_MODELS` → Anthropic 404；`model not in AGENT_MODELS` → benefit 签名；请求前调用注入的 `_refresh_models_if_needed`
- 转换器四块（镜像 kiro2cc-proxy 结构）：
  1. `anthropic_messages_to_openai(body, resolve_model)`：Anthropic body → OpenAI body（纯函数）
  2. `openai_completion_to_anthropic(result, client_model)`：非流式转换（复用注入的 `_parse_upstream_json` 聚合）
  3. `AnthropicStreamConverter`：流式状态机，输入为修补层产出的「归一化 OpenAI chunk 序列」（finish_reason 必有、content 非空串）
  4. `map_stop_reason` / usage 换算（`prompt_tokens`→`input_tokens`、`completion_tokens`→`output_tokens`）
- **层间契约**：修补层输入上游原始字节流，输出归一化 OpenAI chunk 字典序列；转换器假设输入合规；两层各自单测
- 断流兜底：上游断流或缺 finish_reason 时，修补层先补合成 finish chunk（同现有 `generate()` 语义）→ 转换器正常收尾（close 当前块 + `message_delta(stop_reason)` + `message_stop`），Claude Code 不会挂死
- 验证依赖：单元测试用 pytest，新增 `requirements-dev.txt`（pytest）；生产依赖 `requirements.txt` 不变

## 预期影响

- 现有 OpenAI 端点零行为变化（`anthropic_proxy.py` 纯新增，`server.py` 只在末尾追加注册代码）
- Claude Code 接入：`ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_AUTH_TOKEN=codearts claude`（token 任意非空，代理不校验）
- haiku 档默认映射免费 benefit 模型，Claude Code 后台小任务（标题/摘要）不消耗套餐额度

## 风险

- **流式状态机块切换复杂**：thinking/text/tool_use 三类块切换需输出 stop/start 对，参照 kiro2cc-proxy `ChatStreamConverter` 镜像实现，单测覆盖三类块切换与断流兜底
- **层间契约边界**：修补层与转换器两层各自单测，边界以「归一化 OpenAI chunk 序列」为契约
- **事件顺序敏感**：`message_stop` 前必须有带 `stop_reason` 的 `message_delta`；严格按 Anthropic 官方事件顺序输出，异常路径兜底补齐
- **双模块实例风险**：已通过依赖注入规避（见技术方案），禁止任何形式的延迟 `import server`
- **tools 格式待验证**：首选 OpenAI `tools` 真实调用验证；失败再降级 `functions`
- **tool_result+text 混合排序**：按「tool 先、text 后」重排，已定死规则避免实现时临场抉择
