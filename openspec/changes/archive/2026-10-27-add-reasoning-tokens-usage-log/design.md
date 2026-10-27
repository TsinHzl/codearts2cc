## 上下文

使用日志链路四层：采集（server.py OpenAI 端点 `_record_usage` / anthropic_proxy.py `_record_usage`）→ 存储（usage_log.py，`_TOKEN_KEYS` 驱动 summary / by_model / daily 三处聚合）→ admin API（admin_ui.py `_price_entries` 注入 account / cost / credits / saved）→ 前端（UsageTab.tsx）。上游 CodeArts 聊天补全的 usage 是否下发 `completion_tokens_details.reasoning_tokens` 未实测；DeepSeek benefit 模型的思考内容以 `reasoning_content` 增量下发（流式 delta / 非流式 message 字段），历史日志（2846 条）均无 reasoning_tokens 字段。

## 目标 / 非目标

**目标：**
- 每条新日志可观测思考 token 数（精确值优先，估算兜底）
- 聚合三处口径自动覆盖，历史数据兼容
- 转发链路（客户端可见响应）零变化

**非目标：**
- 计价不感知该字段（思考 token 已含于 output_tokens）
- 不回填历史、不修改上游请求体

## 决策

**D1 采集口径（单一提取函数）**
优先级：`usage.completion_tokens_details.reasoning_tokens`（isinstance int、非 bool 且 ≥0）→ `reasoning_chars // 4` → 无值。两来源统一截断 ≤ output_tokens（output_tokens=0 时为 0）。实现为纯函数（usage dict + reasoning 字符数 + output_tokens → int | None），双端点共用，保证 spec「双端点一致」场景单一实现。

**D2 reasoning 字符数来源（按端点 × 流式分四路）**
- OpenAI 非流式：`_parse_upstream_json` 聚合 result 的 `choices[0].message.reasoning_content`（聚合层已拼接，`server.py:545-546`）
- OpenAI 流式：generate 循环内新增本地累计变量，收集 `delta.reasoning_content` 增量；上游误回 JSON 分支取聚合 result。仅累计，不改写转发的 chunk
- Anthropic 非流式：`parse_upstream_json_fn(upstream)` result 同上
- Anthropic 流式：`AnthropicStreamConverter.feed()` 在 reasoning 提取点（`thinking_as_text` 与原生 thinking 两分支共用处）累计字符数（新增只读属性 `reasoning_chars`），`finish()` 后读取；累计不受 `thinking_as_text` 开关影响、不受限额丢弃影响（与输出路径解耦，仅跟随 `feed()` 输入流）

**D3 条目缺省省略键**
两处 `_record_usage` 仅在提取结果非 None 时写入 `reasoning_tokens` 键。理由：与既有历史记录形态一致（`cache_creation_tokens` 在旧记录中缺省），前端以 `'reasoning_tokens' in entry` 区分「–」与 0；聚合层 `_token_value` 缺键按 0 不受影响。

**D7 归档规格归属与枚举同步**
本次前端展示规格落在新建的 `specs/admin-ui/` delta（MODIFIED）而非 usage-log，理由：baseline 中界面列规格（「使用日志思考强度列」「使用日志每日统计视图」）归属 admin-ui capability；同时 delta 显式 MODIFIED 两份 baseline 的字段 / 列枚举（`usage-log` 的 token 字段枚举、`admin-ui` 的每日表列清单），避免归档后规格描述滞后于实现（上一轮 `add-usage-effort-column` 未同步枚举的滞后已被本轮核验标记，本次一并纠正）。

**D4 聚合走 `_TOKEN_KEYS`**
`usage_log.py` `_TOKEN_KEYS` 追加 `"reasoning_tokens"`，summary / by_model / daily 三处聚合、启动回放、滚动重建全部自动覆盖，不新增任何聚合分支代码。语义上该字段是 output_tokens 子集，聚合展示时注意它是「其中思考」而非新增总量（前端文案体现）。

**D5 前端展示位置**
- 明细行：TOKEN 用量列在「缓存写入」与「输入总计」之间插一行「思考 Tokens: N / –」（列已是多行明细样式，不加新列避免挤压表宽）
- 分组表 / 每日表：追加「思考」列（表格加列成本低）
- 「本页 Token」卡片 note：追加「思考 X（Y%）」，Y = 思考小计 / 本页输出小计（输出为 0 时仅显示小计）
- TypeScript 接口四处（Summary / GroupStat / UsageEntry / DailyRow）加 `reasoning_tokens?: number`

**D6 dist 入库**
`npm run build` 后整体替换 `admin-ui/dist/`（沿用 align-admin-ui-codex2cc 确立的「dist 唯一提交点」约定）；`test_admin_ui.py` 断言已是运行时正则提取资源路径，无需修订。

## 风险 / 权衡

| 风险 / 权衡 | 处置 |
|---|---|
| 估算口径（÷4）对中文思考文本系统性偏低估（中文约 1 字 ≈ 0.6–0.7 token，而 ÷4 折算为 0.25 token/字） | 已接受：估算值仅作「有无思考量级」观测，截断规则防荒谬值；上游未来下发精确细分时自动切换（D1 优先级） |
| 流式累计器内存：超长思考流 | 思考文本单段 256 KiB 限额已存在于 converter（anthropic_proxy 既有约束）；OpenAI 端点累计变量同理量级受上游响应约束，仅计字符数即可（`len` 累加，不持有整段文本——注意：OpenAI 流式累计只累加长度，不拼接全文） |
| 上游细分字段值可能为负或非 int | 提取函数按「isinstance int 且非 bool 且 ≥0」校验，非法视为无值走估算 |
| 历史记录无键导致前端「思考」列全「–」 | 验收标准内声明为预期行为（不回填） |

## 迁移方案

无破坏性迁移：新条目多一个可选键，旧读取方（回放聚合 / admin API / 前端）对缺键均已兜底。dist 重新构建即完成前端切换。

## 待决问题

无——数据源口径与聚合范围已在阶段 0 由用户确认（上游字段 + 估算兜底；明细 + 全部聚合）。
