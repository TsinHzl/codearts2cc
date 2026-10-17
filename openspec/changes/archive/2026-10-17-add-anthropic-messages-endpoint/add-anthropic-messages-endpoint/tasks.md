# 任务清单：add-anthropic-messages-endpoint

## 状态：ARCHIVED

## 任务
- [x] T1 请求转换器 `anthropic_messages_to_openai(body, resolve_model)`：system（string/blocks）→ system 消息；content blocks（text/tool_use/tool_result/image）→ OpenAI messages；**tool_result → 独立 `role:"tool"` 消息逐条独立；user turn 内混合块按「tool 先、text 后」重排（text 合并为一条 user 消息排在全部 tool 消息后）**；tools → OpenAI `tools` 格式（待验证假设）；stop_sequences → stop；`supports_images=false` 收到 image block 抛 `invalid_request_error`；验证：单元测试覆盖纯文本/带工具/多轮交替/图片拒绝/混合块排序 5 例
- [x] T2 模型映射（三步法）：剥 `[4m]` 后缀 → `CODEARTS_ANTHROPIC_MODEL_MAP` 别名表 → `claude-*` 未命中兜底 `SUPPORTED_MODELS[0]`+警告；非 `claude-*` 透传交由路由校验；验证：单元测试覆盖 [4m] 剥离/别名命中/未命中兜底/非 claude 透传 4 例
- [x] T3 非流式响应转换器 `openai_completion_to_anthropic(result, client_model)`：content blocks（text/thinking/tool_use）、`map_stop_reason`（stop→end_turn、tool_calls→tool_use、length→max_tokens、content_filter→refusal、未知→end_turn+警告）、usage 换算、上游错误 → Anthropic 错误格式；验证：单元测试覆盖纯文本/工具调用/上游错误 3 例
- [x] T4 流式状态机 `AnthropicStreamConverter`：OpenAI chunk → Anthropic SSE 事件序列；usage 位置：`message_start` 带 `input_tokens`、`message_delta` 带累计 `output_tokens`；thinking/text/tool_use 块切换输出 stop/start 对；tool_calls index ↔ block index 映射；断流兜底 `finish()` 补齐 stop_reason+message_stop；验证：单元测试覆盖文本流/工具调用流/thinking+text 混合流/断流兜底 4 例
- [x] T5 路由与修补层：蓝图 `POST /v1/messages` + **依赖注入**（`AGENT_MODELS` 传引用、`SUPPORTED_MODELS`/`MODEL_DETAILS` 传 getter、`_hmac_headers`/`_parse_upstream_json` 传函数引用；禁止延迟 import server）+ 模型校验/`_refresh_models_if_needed`/通道判断自行实现 + 上游 POST + SSE 逐行修补（补 finish_reason、content:null、other 归一、stream=true 返 JSON 先聚合、流式业务错误转 Anthropic error 事件）+ 按 stream 分派 T3/T4 + SSE headers/CORS；`server.py` 末尾注册；验证：`pytest test_anthropic_proxy.py` 全绿 + 启动 server 后 curl 非流式+流式各 1 次真实调用成功（含 1 次带 tools 的调用验证 tools 格式假设）
- [x] T6 集成验证：`ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_AUTH_TOKEN=codearts claude -p "只回复：成功"` 走通；**工具回环**：自然语言让 Claude Code 运行 `echo ok`，验证 tool_use → 本地执行 → tool_result → 第二轮响应完整回环；验证：两条命令退出码 0
- [x] T7 文档与 dev 依赖：新增 `requirements-dev.txt`（pytest）+ README.md Claude Code 接入段落 + CLAUDE.md 架构段更新；验证：文档与实现一致，`pip install -r requirements-dev.txt` 后 pytest 可执行

## 验收标准
- [x] Claude Code 通过 `ANTHROPIC_BASE_URL` 指向本代理可完成流式与非流式对话
- [x] 工具调用（tool_use/tool_result）双向转换后 Claude Code 可正常执行本地工具并回传结果
- [x] 现有 OpenAI 端点 `/v1/chat/completions` 行为零变化（现有 `python test_openai.py` 通过）
- [x] 单元测试全部通过（`python -m pytest test_anthropic_proxy.py`，覆盖 T1-T4 各转换器）
