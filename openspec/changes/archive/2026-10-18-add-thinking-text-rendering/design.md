## 上下文
`anthropic_proxy.py` 已把上游 `reasoning_content` 转为 Anthropic thinking 块：流式由 `AnthropicStreamConverter.feed()` 输出 `thinking_delta`，非流式由 `openai_completion_to_anthropic()` 输出 thinking 内容块。请求侧 `anthropic_messages_to_openai()` 对 assistant 历史中的 thinking 块直接跳过。本次在该链路上增加可选的 thinking→text 改写。

## 目标 / 非目标
- 目标：Claude Code 客户端内联展示灰色思考文本；开关可回退；其他客户端零影响
- 非目标：改变上游 reasoning_content 的获取/聚合；OpenAI 端点；legacy 格式兼容

## 决策
1. **在 Anthropic 事件层改写，而非字节层**：`AnthropicStreamConverter` 直接生成事件字节，若在字节层二次解析 SSE 会增加解析成本与出错面。选择在 converter 内扩展状态机，复用现有块索引与事件构造。
2. **独立块类型 `thinking_text`**：不复用 `_open_block("text")`，避免与正常回答的 `_block_type == "text"` 分支混淆。`_open_block` 新增 `thinking_text` 分支，输出 `{"type": "text", "text": ""}`。收尾统一在 `feed()` 中处理：进入 content / tool_calls / finish_reason 处理前，若 `_block_type == "thinking_text"`，先 `_flush_thinking_tail()`（尾行 + 时长行）再 `_close_block()`；`finish()` 同样先收尾。
3. **逐完整行 flush**：按 `\n` 切分输出，首行前发 `💭 Thinking` 头行，每行用 `\x1b[2m...\x1b[0m` 包裹；换行前导而非尾随，避免块末尾空行。**取舍**：模型长时间不换行时，该行内容延迟到换行或段结束才显示（与 cc-proxy 一致），不引入半行超时 flush。
4. **每段独立计时**：每个 thinking_text 段在首个 reasoning delta 到达时记录 `time.monotonic()`，段结束时用该段耗时生成时长行。不做请求级共享锚点，避免多段思考时后续段把前文时间计入。
5. **历史剥离在路由层**：新增 `strip_thinking_text_from_messages(messages)`，UA 匹配时在 `messages()` 路由中原地剥离 `body["messages"]`，随后 `anthropic_messages_to_openai(body)` 与 `cache_sim.estimate_body_tokens(body)` / `build_profile(body)` 使用同一份剥离后数据，保证缓存计量与实际 prompt 一致。`anthropic_messages_to_openai` 保持无新参数。
6. **UA 门控 + 配置开关双条件**：响应改写条件为 `is_claude_code_user_agent(UA) and config != "0"`；历史剥离仅由 UA 决定，不随开关变化，防止用户中途切换开关后历史残留文本再次进入上下文。
7. **限额按 UTF-8 字节计**：单段 256 KiB、响应累计 1 MiB，与 cc-proxy 常量一致；计数对象为原始 `reasoning_content`（不含 ANSI 与头行开销）。
8. **多段思考各成一块**：每个上游 thinking 段（reasoning 连续段被 content 或 tool_calls 打断即结束）对应一个 text 块与一个时长行，与 cc-proxy 的 pendingBlock 生命周期一致。
9. **限额边界定稿**：单段超限时丢弃缓冲区中未输出的半行；已 started 则立即 close（不补时长行），未 started 则整段吞掉。响应累计超限时，若当前有活跃 thinking_text 块则立即 close（不补时长行），后续段完全吞掉（不产生任何事件）。非流式不限额。
10. **格式常量**：`ANSI_DIM = "\x1b[2m"`、`ANSI_RESET = "\x1b[0m"`、头行 `💭 Thinking`、时长行 `💭 Thought for {label}`；`label` 为 `{n}s`（n < 60）否则 `{n//60}m{n%60}s`，`n = max(1, math.floor(seconds + 0.5))`（对齐 cc-proxy 的 JS `Math.round`；Python 内建 `round()` 为银行家舍入，`.5` 边界结果不同，禁止使用）。行处理：`\r\n?` → `\n`，去尾部空行，逐行 `strip()` 后跳过空行（缩进丢失，与 cc-proxy 一致）。**时长行后必须补尾随 `\n`**：Claude Code 的 recap 会把相邻 text 块拼接，缺尾随换行时时长行与后续回答渲染在同一行（cc-proxy `thinking-text.js:280` 同样补 `\n`）。
11. **ESC 控制序列净化**：上游 `reasoning_content` 由模型生成，可能包含 ESC 序列导致灰色提前终止或终端转义注入。输出前用正则剥离三类序列，再包裹 dim/reset：
    - OSC：`\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)`
    - CSI：`\x1b\[[0-9;?]*[ -/]*[@-~]`
    - 其余两字符转义：`\x1b[@-_]`

    OSC 必须排在最前（`]` 落在 `[@-_]` 范围内，顺序错误会只吃掉 `ESC ]` 而把 payload 当正文输出）。
12. **非流式格式**：复用 `format_thinking_as_text()`，输出头行 + 灰色行，**不含时长行**（非流式无可信计时锚点）。该格式与历史剥离正则匹配，保证非流式轮次产生的历史同样可被剥离。

## 风险 / 权衡
- 状态机新增 `_thinking_text_active` / `_thinking_pending` 等状态，若收尾遗漏会产生孤立 stop；所有收尾路径（content / tool_calls / finish_reason / finish()）统一调用 `_finish_thinking_text()`
- 限额丢弃后思考文本不完整，属预期取舍
- 历史剥离依赖 Claude Code 原样回传 ANSI 与头行；客户端改写历史时剥离失效，思考文本作为普通上下文转发
- 逐行 `strip()` 丢失缩进，思考文本中的代码块格式不保留
- `admin-ui` 现有 spec 的「运行时配置编辑」仍描述写回 `.env`，与当前 `config.json` 实现不符；本次不修正，避免范围蔓延（记录为已知文档债务）

## 迁移方案
无数据迁移；新增配置键缺省视为开启，无需用户操作。

## 待决问题
无（格式、限额、多段思考、tool_use 收尾、缓存口径已在本节定稿）
