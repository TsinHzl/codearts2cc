## 上下文
- 使用日志的 `effort` 字段早已存在于存储与 API 层（`usage_log.py` 为自由字段、`admin_ui._price_entries` 原样透传），本次不改存储结构。
- Anthropic 端点当前从 `thinking.effort` 提取（`anthropic_proxy.py:129`），透传侧则在同一位置附近另有一份「取 thinking.effort → 缺省补 high」的重复逻辑（`:487-490`），两处口径可能漂移。
- 前端 `UsageTab.tsx` 已有 `effort?: string` 类型与模型列下方的灰色小字展示，无独立列。
- 参考实现：`../kiro2cc-proxy` 的 `resolve_recorded_effort(thinking_effective, effort)` —— thinking 生效时保留客户端值、未携带补 `high`，未生效一律丢弃。

## 目标 / 非目标
**目标：** 让「思考强度」成为表格的一等列；让日志 effort 与上游 `reasoning_effort` 同源。
**非目标：** 不做按 effort 的聚合/筛选；不引入 thinking 生效与否的账号级开关（本仓库无账号池概念）；不回填历史数据。

## 决策
**D1. 单一解析函数作为唯一真源。** 新增 `resolve_effort(body)` 供记录与透传两处共用，避免再次出现两份口径。取值优先级 `output_config.effort` → `thinking.effort`，与原透传逻辑「thinking.effort → 缺省 high」相比，仅增加一个更高优先级的来源，向后兼容。

**D2. 采纳 kiro2cc「未携带补记 high」的做法。** `../kiro2cc-proxy` 的
`resolve_recorded_effort(thinking_effective, effort)` 在思考生效时保留客户端值、未携带补记 `high`、
未生效一律丢弃。本仓库沿用同一口径：日志记录值 = 思考未关闭时 `resolve_effort(body) or "high"`，
`thinking.type=="disabled"` 时为 null。好处是「日志里的 effort」与「上游实际收到的 reasoning_effort」
永远一致，界面不会出现「显示 –、实际按 high 推理」的误导。代价是绝大多数未设置 effort 的请求
（含当前 2555 条历史记录中的 2549 条 null）在新写入时统一显示 `high`，无法区分「用户没设置」与
「用户设置了 high」——该区分能力被明确放弃，以换取口径统一。历史 null 记录展示「–」，不回填。

**D3. disabled 判定放在解析函数内。** `thinking.type=="disabled"` 时 `resolve_effort` 返回空，
透传侧沿用已有的「disabled 提前 return、不发 reasoning_effort」分支，记录侧由
`resolve_recorded_effort` 在该条件下直接返回 null（不补 high），两处语义一次对齐。

**D4. 前端独立列而非徽章。** 用户明确要求「每条日志新增思考强度列」；列位置在模型列之后，与端点/流式的请求属性归为同一阅读区。原模型列下方的 effort 小字删除，避免同一信息出现两次。空值统一用「–」，与积分、首字等列的空值口径一致。

**D5. 不改 `usage_log.py`。** effort 不是聚合维度，`query()` 原样返回条目，无需改动存储与统计逻辑。

## 风险 / 权衡
- API 响应结构不变，仅 `entries[].effort` 的取值来源扩展，前端向后兼容。
- 「未携带补记 high」使新写入记录在未设置 effort 时显示 `high`，与历史 null 记录（显示「–」）
  形成视觉差异；这是口径从「客户端表达」转为「实际生效值」的必然结果，已在验收标准中明确。
- 前端产物入库（`admin-ui/dist/`），必须重新构建，否则界面与源码不一致。
- 新列使明细表从 7 列变 8 列，窄屏横向滚动范围略增；表格容器已有 `overflow-x-auto`，无需额外适配。

## 迁移方案
无数据迁移。旧记录 `effort` 为 null，新列显示「–」。回滚只需还原源码并重新构建 dist。
