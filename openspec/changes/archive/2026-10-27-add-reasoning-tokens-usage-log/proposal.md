# 变更提案：add-reasoning-tokens-usage-log

## 背景

使用日志当前每条记录仅含输入 / 输出 / 缓存读取 / 缓存写入四类 token 用量，深度思考（reasoning）产生的 token 数量没有单独呈现。上游聊天补全响应的 `usage` 中可能携带 `completion_tokens_details.reasoning_tokens` 细分（OpenAI 兼容惯例），但代理在 `_record_usage` 组装日志条目时只挑选固定字段入库，该细分被丢弃；同时部分模型（如 DeepSeek benefit 流）的思考内容以 `reasoning_content` 增量下发且 usage 不带细分字段，上游字段缺失时完全没有可观测数据。

管理员在「使用日志」页无法回答「这次请求花了多少思考 token」。

## 目标范围

**在范围内：**
- 采集层：OpenAI 端点与 Anthropic 端点的 `_record_usage` 在组装日志条目时，提取 `usage.completion_tokens_details.reasoning_tokens` 作为思考 token 数（`reasoning_tokens` 可选字段）
- 采集层兜底：上游 usage 无 reasoning_tokens 细分时，若响应携带非空 `reasoning_content`，按 `字符数 ÷ 4` 本地估算并记入同一字段（估算值，非上游精确值）
- 累计器口径：Anthropic 流式累计器加在 `AnthropicStreamConverter.feed()` 的 reasoning 提取点（`thinking_as_text` 与原生 thinking 两分支共用处），不受 `thinking_as_text` 开关影响、不受思考限额丢弃影响；OpenAI 流式累计器在转发循环内独立累计（仅记字符数，不持有全文、不改写转发内容）
- 截断对称性：上游细分与本地估算两个来源统一截断为 ≤ `output_tokens`，上游细分脏数据（大于 completion_tokens）同样被截断
- 存储层：`usage_log.py` 将 `reasoning_tokens` 纳入 `_TOKEN_KEYS` 聚合键，summary / by_model / daily 三处聚合自动覆盖；同步模块级 `_summary` 初始化字面量
- 展示层：`UsageTab.tsx` 请求日志明细行 TOKEN 用量列新增「思考 Tokens」一行；汇总卡片「本页 Token」note 追加思考小计与占比；按模型分组表与每日统计表新增「思考」列
- 前端重新构建 dist/ 并入库（`npm run build`）
- 测试：提取函数（上游细分 / 估算兜底 / 截断 / 非法值）、聚合、双端点条目键断言、前端展示兼容历史无字段记录

**不在范围内：**
- 不修改计价逻辑（思考 token 属于输出 token 的子集，已含在 `output_tokens` 内，不重复计价；`pricing.py` 不感知该字段）
- 不修改转发给客户端的 OpenAI / Anthropic 响应体（保持既有转换逻辑不变）
- 不回填历史日志（历史记录无该字段，前端明细行显示「–」）
- 不修改上游请求体构造（`reasoning_effort` 透传逻辑不变）

## 技术方案

- **提取优先级（单口径共享函数）**：`usage.completion_tokens_details.reasoning_tokens`（isinstance int、非 bool 且 ≥0 时）→ 有非空 `reasoning_content` 时 `len(reasoning_content) // 4` 估算 → 皆无则返回 None（条目中省略键，与 `cache_creation_tokens` 在旧记录中缺省的处理一致）；两个来源的最终值统一截断 ≤ `output_tokens`（`output_tokens` 为 0 时记 0）
- **Anthropic 端点数据源**：非流式从 `parse_upstream_json_fn` 聚合 result 的 `reasoning_content` 提取；流式由 `AnthropicStreamConverter` 在 `feed()` reasoning 提取点累计字符数（新增只读属性），`finish()` 后读取
- **OpenAI 端点数据源**：非流式从 `_parse_upstream_json` 聚合 result 提取；流式在 chunk 转发循环中累计 `delta.reasoning_content` 长度（含上游误回 JSON 包装为单帧流的分支）；仅累计，不修改转发内容
- **存储**：`reasoning_tokens` 加入 `_TOKEN_KEYS`，summary / by_model / daily、启动回放与滚动重建自动覆盖；同步修正模块级 `_summary` 字面量（现硬编码四个键，需含 `reasoning_tokens`）与 `_by_model` 注释；历史条目缺字段按 0 累计（`_token_value` 已兜底）
- **前端**：明细行在「缓存写入」与「输入总计」之间插入「思考 Tokens」行，缺失显示「–」；分组表 / 每日表追加「思考」列，缺失按 0 展示（聚合计数语义，与缓存写入列一致）；「本页 Token」卡片 note 追加思考小计
- **约束**：`reasoning_tokens` 语义上 ≤ `output_tokens`（思考是输出的子集）

## 预期影响

- 使用日志 JSONL 新条目多一个可选键，旧文件回放 / 新文件读取双向兼容（缺失按 0）
- `_TOKEN_KEYS` 扩展导致启动回放聚合时新键按 0 累计历史记录，summary 思考数只统计新记录
- 转发链路零改动：累计器只读（Anthropic 侧在 converter 内部、OpenAI 侧为局部变量），不触碰 chunk / result 原文，客户端可见响应不变
- dist/ 重新构建入库，`test_admin_ui.py` 既有断言不依赖具体资源名（已改为正则提取），无需修订

## 风险

| 风险 | 应对 |
|---|---|
| 上游不返回 `completion_tokens_details`，列值长期靠估算 | 已接受（用户选定方案）：估算仅作量级观测，上游未来提供精确值时自动切换（提取优先级保证）；口径偏差已在 CLAUDE.md 说明中声明 |
| 估算系数 ÷4 对中文思考文本系统性偏低估（CJK 约 1.5–2 字符/token） | 已接受：文档口径注明「÷4 为英文近似、中文偏低估」，防止被当作精确值 |
| 流式累计 reasoning 增量引入转发性能开销 | 仅整数长度累加（不拼接全文、无额外 IO）；DeepSeek 思考流每秒数十 delta 量级，开销可忽略 |
| 前端三处表格 + 明细行遗漏导致口径不一致 | tasks.md 中展示点分项列出，CR 时按 spec 场景逐一核对 |
| 滚动阈值与聚合重建 | 无新增风险：`reasoning_tokens` 走既有 `_TOKEN_KEYS` 通道，滚动重建自动覆盖 |
