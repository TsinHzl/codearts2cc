# 变更提案：add-thinking-text-rendering

## 背景
CodeArts 上游的 `reasoning_content` 目前被转换为 Anthropic 原生 `thinking` 内容块，Claude Code 会将其折叠为 Thinking 摘要，用户无法直接阅读思考过程。同级项目 cc-proxy 通过把 thinking 块改写为带 `💭 Thinking` 头行的灰色文本流，让思考内容在终端内联展示。本变更借鉴该做法，在 Anthropic Messages 端点实现同等能力。

## 目标范围
**在范围内：**
- 流式 `/v1/messages`：thinking 块改写为灰色文本事件流（逐完整行输出，`💭 Thinking` 头行 + 每段结束的时长行）
- 非流式 `/v1/messages`：thinking 块改写为 text 块（头行 + 灰色行；无时长行、不限额，与 cc-proxy `transformNonStreamingResponse` 一致）
- 仅对 User-Agent 匹配 `claude-cli` / `claude-code` 的客户端生效，其他客户端保持原生 thinking 块
- 新增配置键 `CODEARTS_THINKING_AS_TEXT`（默认开启），管理后台配置页提供开关
- 多轮对话历史中已渲染的思考文本在转发上游前剥离，并同步用于缓存估算输入
- 单块 256 KiB / 单响应 1 MiB 的大小限额保护
- 上游思考文本中的 ESC 控制序列（CSI / OSC / 两字符转义）在输出前剥离，防止终端转义注入
- 思考段时长行末尾补尾随换行，避免 Claude Code recap 把时长行与后续回答渲染在同一行

**不在范围内：**
- OpenAI `/v1/chat/completions` 端点的响应格式
- 上游 `reasoning_content` 的获取与聚合逻辑
- Claude Code 之外客户端的思考展示方式
- 历史遗留（legacy / quoted）格式的剥离（本项目首次实现，无历史格式）

## 技术方案
- 在 `anthropic_proxy.py` 新增 thinking 文本化工具函数与常量（ANSI dim 格式、时长格式化、ESC 净化、历史剥离正则、UA 判定）
- 路由层：UA 匹配时调用 `strip_thinking_text_from_messages()` 原地剥离历史思考文本，再执行 `anthropic_messages_to_openai()` 与 `cache_sim` 估算（两者输入一致，避免缓存计量失真）
- 流式：扩展 `AnthropicStreamConverter`，新增 `thinking_as_text` 参数；reasoning delta 使用独立的 `thinking_text` 块类型，惰性打开 text 块并逐完整行输出；在 content / tool_calls / finish_reason / finish() 四个切换点统一收尾（补尾行、时长行与 `content_block_stop`）；每个思考段独立计时
- 非流式：扩展 `openai_completion_to_anthropic`，新增 `thinking_as_text` 参数，复用同一格式化函数
- 配置键加入 `admin_ui.EDITABLE_KEYS`、`config.example.json` 与 React 配置页下拉

## 预期影响
- 仅影响 Anthropic 端点且 UA 匹配 Claude Code 的客户端；OpenAI 端点与第三方客户端行为不变
- 输出以完整行为单位：模型长时间不换行时，该行内容会延迟到换行或思考段结束时才显示（与 cc-proxy 一致）
- 关闭开关后思考块恢复原生格式；但 Claude Code UA 下历史剥离仍生效，避免残留文本再次进入上下文（cc-proxy 同样以 UA 单独决定历史剥离，不受 `thinkingAsText` 开关控制）
- 仅含空白内容的思考段不产生任何 content_block（惰性打开，与 cc-proxy 一致）
- 历史剥离减少多轮上下文体积，同时使缓存估算与实际 prompt 保持一致
- 非流式响应同样改写：thinking 块替换为独立 text 块，不与回答文本合并

## 风险
- 流式状态机复杂度上升：thinking → text 的惰性块切换若处理不当会产生孤立 `content_block_stop`；通过状态标记与单元测试覆盖
- 大小限额丢弃可能导致思考内容不完整：与 cc-proxy 行为一致，仅影响超长思考
- UA 门控可能误伤自定义 UA 的 Claude Code 分支版本：按 `claude-cli` / `claude-code` 前缀匹配，与 cc-proxy 一致
- 历史剥离依赖 Claude Code 原样回传 ANSI dim/reset 与头行格式；若客户端 sanitize 或改写历史，剥离失效，思考文本会作为普通上下文转发（不影响正确性）
- 文本化后思考内容与回答同为 text 块，在会话历史 / recap 中可能被误认为回答内容（cc-proxy 已接受该取舍）
- 逐行 `strip()` 会丢失缩进（与 cc-proxy 一致），思考文本中的代码块缩进不保留
