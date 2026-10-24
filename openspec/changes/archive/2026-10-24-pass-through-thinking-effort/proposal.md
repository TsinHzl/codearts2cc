# 变更提案：pass-through-thinking-effort

## 背景
当前 Anthropic Messages 端点（`/v1/messages`）接收 Claude Code 请求时，
`anthropic_messages_to_openai` 转换器（`src/proxy/anthropic_proxy.py`）只组装
`model / messages / max_tokens / stream / temperature / top_p / stop / tools / tool_choice`，
**并未把 Claude Code 的 `thinking.effort` 透传到上游 OpenAI Chat Completions 请求体**。
因此无论本地设置何种 effort，上游模型（如 deepseek-v4.1-flash、glm-5.3）
都收不到该参数，思考深度无法通过此通道调节。

实测确认：OpenAI 路径显式传 `reasoning_effort`（low/high/max）上游返回 200 且不报错，
即该字段可安全透传；而 Anthropic 路径当前完全丢弃 `thinking` 参数。

## 目标范围
**在范围内：**
- Anthropic 端点 `/v1/messages`：转换器把请求的 `thinking.effort` 透传到上游
  OpenAI 请求体的 `reasoning_effort` 字段
- 透传语义「透传优先」：Claude Code 请求自带 `thinking.effort` 时透传其原值；
  缺省时补默认值 `high`
- 使用日志 `effort` 字段继续沿用现有提取逻辑（`thinking.effort`），不受影响

**不在范围内：**
- 不修改 OpenAI 路径 `/v1/chat/completions`（该路径已全量透传未知字段，无需改动）
- 不干预上游模型是否实际采纳 effort（上游 `thinking_type` 为空时可能忽略，属上游行为，非本代理责任）
- 不修改 caching / thinking 文本化等既有逻辑
- 不改动默认值来源为配置项（本变更固定默认 `high`，如需可配置后续另立变更）

## 技术方案
在 `src/proxy/anthropic_proxy.py` 的 `anthropic_messages_to_openai` 构建 `openai_body` 时，
新增 effort 透传逻辑：

```python
thinking = body.get("thinking")
effort = thinking.get("effort") if isinstance(thinking, dict) else None
if not (isinstance(effort, str) and effort):
    effort = "high"  # 透传优先：缺省补 high
openai_body["reasoning_effort"] = effort
```

仅当已有 `reasoning_effort` 字段不存在时写入，避免与显式字段冲突（当前转换器不会设置该字段）。
保持纯函数约束：仅对入参 `body` 读取，不产生副作用。

## 预期影响
- Claude Code 用户在 `/v1/messages` 发出的请求，缺省情况下上游将收到
  `reasoning_effort="high"`；自带 effort 时透传原值
- 上游实测对 `reasoning_effort` 接受（200），不引入新 4xx/5xx 风险
- 使用日志 `effort` 列仍显示本地提取值，行为不变

## 风险
- 上游可能忽略 effort（`thinking_type` 为空模型）：本变更仅保证透传，不承诺调档效果，
  属上游语义，不作为缺陷
- `reasoning_effort` 值域（low/medium/high）与 Claude Code `thinking.effort` 兼容，
  均为同一枚举语义，无需映射