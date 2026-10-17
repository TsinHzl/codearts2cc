# anthropic-thinking-rendering Specification

## Purpose
将 CodeArts 上游的深度思考内容以灰色文本形式内联展示给 Claude Code 用户，同时保持其他客户端与关闭开关时的原生 thinking 块行为。

## Requirements

### Requirement: 思考内容文本化
系统 SHALL 在 Anthropic Messages 端点将上游 `reasoning_content` 产生的 thinking 块改写为 text 块：流式响应 SHALL 逐完整行增量输出，非流式响应 SHALL 一次性输出。

#### Scenario: 流式逐行输出
- **GIVEN** 客户端 UA 匹配 Claude Code 且 `CODEARTS_THINKING_AS_TEXT` 未关闭
- **WHEN** 上游返回 thinking 增量
- **THEN** 响应 SHALL 不包含 `thinking` 类型的 content_block
- **AND** 若该思考段含至少一个非空行，SHALL 包含一个 text content_block，首个 text_delta 为 `\x1b[2m💭 Thinking\x1b[0m`
- **AND** 每个非空思考行 SHALL 以 `\n\x1b[2m<trimmed line>\x1b[0m` 输出，空行 SHALL 跳过
- **AND** 思考段结束时 SHALL 追加 `\n\x1b[2m💭 Thought for <label>\x1b[0m\n`（尾随换行防止 recap 与后续回答同行；不足 60 秒为 `Ns`，否则 `NmSs`；四舍五入 = `floor(seconds + 0.5)`，最小 1 秒）
- **AND** 若思考段仅含空白内容（无任何非空行），SHALL NOT 产生 content_block_start / stop
- **AND** 思考块 SHALL 在正常回答 / tool_use / finish 之前关闭，块索引连续

#### Scenario: 思考后直接调用工具
- **GIVEN** 上游在 reasoning 增量后直接返回 tool_calls
- **WHEN** 流式响应处理该序列
- **THEN** 系统 SHALL 先补尾行与时长行并关闭 thinking_text 块
- **AND** SHALL 再打开 tool_use 块，SHALL NOT 产生孤立 content_block_stop

#### Scenario: 非流式改写
- **GIVEN** 客户端 UA 匹配 Claude Code 且开关未关闭
- **WHEN** 非流式请求返回含 `reasoning_content` 的响应
- **THEN** 响应 content SHALL 包含一个独立 text 块，内容为 `\x1b[2m💭 Thinking\x1b[0m` 头行 + 各灰色行
- **AND** SHALL NOT 包含时长行（非流式无可信计时锚点，与 cc-proxy `transformNonStreamingResponse` 一致）
- **AND** SHALL NOT 施加大小限额（与 cc-proxy 一致）
- **AND** 正常回答文本 SHALL 保持为独立的 text 块，两者不合并

#### Scenario: 上游思考内容含控制序列
- **GIVEN** 上游 `reasoning_content` 包含 ESC 控制序列（CSI 如 `\x1b[31m`、OSC 如 `\x1b]0;title\x07`、两字符转义如 `\x1bM`）或裸 C0/C1 控制字符
- **WHEN** 系统文本化该内容
- **THEN** 输出 SHALL 剥离上述三类控制序列与裸控制字符（保留 `\t` / `\n` / `\r`）
- **AND** SHALL NOT 让上游内容提前终止 ANSI dim 或注入其他终端控制码

### Requirement: 生效范围与配置开关
系统 SHALL 仅在 User-Agent 匹配 `claude-cli` 或 `claude-code` 前缀时改写思考内容；配置键 `CODEARTS_THINKING_AS_TEXT` 设为 `0` 时 SHALL 保持原生 thinking 块行为。

#### Scenario: 非 Claude Code 客户端不受影响
- **WHEN** UA 不匹配正则 `^(?:claude-cli|claude-code)(?:/|\s|$)`（不区分大小写）的客户端请求 Anthropic 端点
- **THEN** thinking 块 SHALL 保持原生格式
- **AND** 历史消息 SHALL NOT 被剥离

#### Scenario: 开关关闭
- **GIVEN** `CODEARTS_THINKING_AS_TEXT=0`
- **WHEN** Claude Code 发起请求
- **THEN** thinking 块 SHALL 保持原生格式
- **AND** 管理界面 SHALL 提供该开关的编辑入口
- **AND** 配置键缺失或取 `0` 以外的值 SHALL 视为开启（`0` / `false` / `off` / `no`，不区分大小写，视为关闭）
- **AND** Claude Code UA 下历史剥离 SHALL 仍然生效（该行为不受开关控制）

### Requirement: 历史思考文本剥离
系统 SHALL 在转发上游前剥离 assistant 历史消息中已渲染的思考文本（头行、灰色行与时长行），避免上下文膨胀；剥离后的消息 SHALL 同时作为缓存估算的输入。

#### Scenario: 多轮对话剥离
- **GIVEN** 历史 assistant 消息的 text 块包含 `💭 Thinking` 文本化内容
- **WHEN** 客户端发起下一轮请求
- **THEN** 转发上游的消息 SHALL 不含该思考文本
- **AND** 同一条消息中的正常回答文本 SHALL 保留

#### Scenario: 思考与回答拼接在同一 text 块
- **GIVEN** 历史 assistant 消息的单个 text 块同时包含思考文本与正常回答
- **WHEN** 客户端发起下一轮请求
- **THEN** 思考文本 SHALL 被剥离
- **AND** 回答文本 SHALL 完整保留

#### Scenario: 缓存估算同步
- **GIVEN** 历史思考文本被剥离
- **WHEN** 系统计算缓存用量估算
- **THEN** 估算输入 SHALL 为剥离后的消息
- **AND** SHALL NOT 按未剥离的原始 body 计算

### Requirement: 思考文本大小限额
系统 SHALL 对文本化思考内容施加上限：单个 thinking 段的原始思考文本（UTF-8 字节）超过 256 KiB，或单次流式响应累计超过 1 MiB 时，SHALL 停止文本化。

#### Scenario: 单块超限
- **GIVEN** 单个 thinking 段的思考文本超过 256 KiB
- **WHEN** 流式响应处理该段
- **THEN** 系统 SHALL 丢弃缓冲区中尚未输出的半行
- **AND** 若该段已打开 text 块，SHALL 立即关闭且不补时长行；若未打开，SHALL 完全吞掉该段（无 start/stop）
- **AND** SHALL NOT 产生孤立 content_block_stop

#### Scenario: 响应累计超限
- **GIVEN** 单次响应文本化的思考内容累计超过 1 MiB
- **WHEN** 后续 thinking 段到达或当前段仍在输出
- **THEN** 系统 SHALL 停止文本化
- **AND** 若当前有活跃 thinking_text 块，SHALL 立即关闭且不补时长行
- **AND** 后续 thinking 段 SHALL 完全吞掉（无 content_block_start / stop）

#### Scenario: 多段思考
- **GIVEN** 上游 reasoning 分多个不连续的 thinking 段到达
- **WHEN** 流式响应处理
- **THEN** 每个 thinking 段 SHALL 对应一个独立 text 块
- **AND** 每段时长 SHALL 从该段首个 reasoning delta 起算，各自输出一次时长行

#### Scenario: 非流式不限额
- **WHEN** 非流式响应包含 thinking 块
- **THEN** 系统 SHALL 不施加大小限额（见「非流式改写」场景）
