## 上下文

现有 `usage_log.py`（130 行）只维护「全量聚合 + 最近 200 条明细」，`query()` 无日期参数；`admin_ui.py` 的 `EDITABLE_KEYS` 只有两个键；`UsageTab.tsx`（199 行）为 4 个 token 卡 + 按模型表 + 11 列明细。上游 CodeArts 响应不含计费字段，费用只能本地算。

## 目标 / 非目标

**目标：** 按截图重构使用日志页；明细保留全量；支持日期区间与每日聚合；费用/Credits/缓存节省额由可配置单价表计算；账号名可配置。

**非目标：** IP 归属地、失败请求记录、导出、图表、多账号、上游计费对接。

## 决策

### D1：新增独立 `pricing.py` 模块
解析 `CODEARTS_PRICING`（JSON 字符串 → dict）、校验数值为有限非负实数（`math.isfinite`，拒绝布尔值）、提供 `load_table()` 返回解析结果与警告、`compute(entry, table, rate)` 返回 `cost` / `credits` / `saved`（未命中模型返回 `None`）。不把计价逻辑塞进 `usage_log.py`，保持存储与计价职责分离，便于单测。`load_table()` 由 API 层每次查询调用一次并复用，不在每条记录上重复解析；保存后下一条查询立即生效。

### D2：全量明细
删除 `_MAX_ENTRIES` 与内存裁剪；`_entries` 保留文件内全部记录。`init()` 回放后不再 `[-200:]`。新增模块级计数器 `_file_lines`：`init()` 时设为成功解析的条目数，`record()` 每成功追加一条加 1。`record()` 先追加写入文件与 `_entries`，再判断是否超过 `_MAX_FILE_ENTRIES`；超阈值时在写锁内保留较新一半、原子重写文件、重置计数器，并基于保留集合同步重建 `_summary` 与 `_by_model`。`_file_lines` 统计成功解析条数而非物理行数，残损行不影响阈值。

### D3：日期过滤在 `query()` 内完成
新增参数 `start` / `end`（`YYYY-MM-DD`）与 `daily`。过滤用 `entry["ts"][:10]` 字符串比较（`ts` 为本地时间 `isoformat`），无时区换算。API 层先用正则 `^\d{4}-\d{2}-\d{2}$` 拒绝非补零格式，再用 `datetime.strptime` 校验真实日期；非法或 `start > end` 返回 400。`query()` 只接收已校验值，并对缺失 `ts` 的历史条目按空串处理（不匹配任何区间）。`daily` 接受 `true` / `1`（大小写不敏感），其余取值（含缺省、`false`、`0`）视为关闭；关闭时响应中 `daily` 为 `null`。区间内无记录的日期不补零，只返回有记录的日期。

### D4：每日聚合
`daily=true` 时按 `ts[:10]` 分组，返回升序列表，每项只含 `date` / `requests` / `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens`；同时返回 `daily_models`（按日期 + 模型聚合的 token 桶），供 API 层按桶计价后线性累加。聚合在**过滤后的全量集合**上做，不是当前页。`cost` / `credits` / `saved` / `unpriced_requests` 由 API 层在原始聚合上补齐，避免 `usage_log` 依赖计价模块。

### D5：账号注入在 API 层
`usage_log.query()` 不依赖 `config_store` 与 `pricing`，返回的 `entries` 中每一项为 `dict(entry)` 副本，防止调用方修改污染 `_entries`。`admin_ui.admin_usage()` 读取 `CODEARTS_ACCOUNT_NAME`（空串或缺失回退「主账号」），为每条明细用 `{**entry, "account": ..., "cost": ..., "credits": ..., "saved": ...}` 构造新对象，并计算 `page_totals` 与每日统计的费用字段。

### D6：单价表格式与容错
`CODEARTS_PRICING` = `{"<上游模型 id>": {"input": 0.1, "output": 0.3, "cache_read": 0.02, "cache_creation": 0.1}}`，单位 USD / 1M tokens。键为 `resolve_model()` 之后的模型 id，不是 Claude 别名。`input` / `output` 必填，`cache_read` / `cache_creation` 缺省时按 `input` 单价计算（避免缓存 token 被计成 0）。整表非法 JSON、非对象、负数、`NaN` / `Infinity`、布尔值 → 视为空表并打印警告；单个模型条目缺必填字段 → 该模型视为未配置。`CODEARTS_CREDITS_RATE` 默认 `"1.0"`，非法、非有限或负数回退 1.0。

### D7：配置页校验
`CODEARTS_PRICING` 在 `admin_set_config()` 内做专项校验（JSON 可解析、顶层对象、值对象、数值为有限非负实数、拒绝布尔值、含 `input` / `output`），不通过返回 400 且不写盘；其余键沿用现有单行可打印校验。`ConfigTab` 在提交前必须执行 `JSON.stringify(JSON.parse(text))` 压缩多行输入。`CODEARTS_CREDITS_RATE` 校验可解析为有限非负浮点。`admin_get_config()` 对三个新增键返回默认值（账号「主账号」、单价表 `"{}"`、系数 `"1.0"`），避免前端把缺失键写成空串。

### D8：前端布局对齐截图
页头固定为面包屑「主要 / 使用日志」、标题「使用日志」、副标题「按时间维度查看用量与请求明细」。

8 列：时间（固定 `YYYY/MM/DD HH:mm:ss`）/ IP / 账号 / 模型 / TOKEN 用量（多行：输入 Tokens、输出 Tokens、缓存读取、缓存写入、输入总计加粗，输入总计 = 输入 + 缓存读取）/ 用时（`duration_ms`，无值显示「–」，悬浮显示首字耗时）/ 费用（`$` 前缀 + 4 位小数）/ KIRO CREDITS（4 位小数 +「积分」，含「省 x」副行）。端点与流式标签降级为模型列下方小字，不新增列。请求日志默认每页 100 条、上限 200 条。统计卡：总请求数（副行「当页 {n} / 共 {total} 条 | 第 {page} / {pages} 页」）、本页费用（副行「单请求均价 $x.xxxx」，存在未计价时追加「另有 N 条未计价」）、本页 Credits（副行「本页缓存节省额 x.xxxx 积分」或「本页无缓存节省额」）、本页 Token（副行「缓存读取 {n} {pct}%」+ 占比环形 SVG，占比 = 缓存读取 ÷ 输入总计（输入 + 缓存读取），保留一位小数，总量为 0 时显示 0%）。时间预设默认「7 天」。

「每日统计」开启时以日聚合表替换请求日志表：列为日期 / 请求数 / 输入 / 输出 / 缓存读取 / 缓存写入 / 费用 / Credits / 节省 / 未计价条数，不分页，空区间显示「该区间无记录」；未计价日的费用 / Credits / 节省显示「–」。切回明细时保留当前区间并将页码重置为 1。

保留现有「按模型分组」表，改为请求日志上方的可折叠面板，默认折叠。

时间预设与自定义区间互斥：点击自定义输入即清除预设高亮；切换任一区间后页码重置为 1。

### D9：不新增前端依赖
环形进度用内联 SVG `<circle>` + `stroke-dasharray`，不引入图表库。

### D10：边界与精度
- 时间列固定 `YYYY/MM/DD HH:mm:ss`（由 `ts` 解析后格式化，不直接渲染 ISO 串）。
- 明细「输入总计」= 输入 + 缓存读取；缓存写入单列展示，不计入该行。
- 「用时」无有效 `duration_ms` 时显示「–」；费用与 Credits 无单价时显示「–」，有值时分别带 `$` 前缀与「积分」单位。
- 环形占比 = 缓存读取 ÷ 输入总计（输入 + 缓存读取），保留一位小数（该口径与截图 84.8% 一致）。
- 请求日志默认每页 100 条，分页副行文案「当页 {n} / 共 {total} 条 | 第 {page} / {pages} 页」。
- 10000 条滚动上限已在阶段 0 确认接受；长期保留需外置存储，不在本次范围。
- 缓存节省额负值截断为 0（缓存单价高于输入单价时不显示负节省）。
- 聚合中的 `cost` / `credits` / `saved` 只累加有单价记录，`unpriced_requests` 给出未计价条数。
- 费用与 Credits 展示固定 4 位小数，token 用千分位；后端不做舍入。
- 「本页均价」= 本页有单价记录的费用合计 ÷ 有单价记录数；本页全部未计价时不显示均价。
- 时间预设均含当天：今日 = 当天；7 天 = 今天及前 6 天；14 / 30 天同理。默认选中「7 天」。
- 历史日志无 `cache_creation_tokens` 时按 0 计价，不报错。
- 运行期滚动后累计请求数、按模型分组与每日统计同步下降，界面不额外提示。
- 本页全部未计价时不显示均价；部分未计价时均价分母只计有单价记录。
- `daily` 关闭时响应字段为 `null`，不是空数组。

### D11：API 响应契约
`GET /api/admin/usage` 返回：

```json
{
  "summary": {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0},
  "by_model": {"<model>": {"requests": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}},
  "entries": [{"ts": "...", "ip": "...", "account": "主账号", "model": "...", "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0, "cost": 0.0, "credits": 0.0, "saved": 0.0}],
  "page": {"page": 1, "limit": 100, "total": 0},
  "page_totals": {"requests": 0, "priced_requests": 0, "cost": null, "credits": null, "saved": null},
  "daily": null,
  "account": "主账号",
  "start": "2026-10-11",
  "end": "2026-10-17"
}
```

`summary` 基于过滤后全量；`by_model` 为对象（模型 id → 聚合，含 `requests` / `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens`），与现有 `usage_log.query()` 返回结构及 `UsageTab.tsx` 消费方式一致，不改为数组；`entries` 为当前页；`page_totals` 只累加有单价记录，全部未计价时三个金额为 `null`；`daily` 关闭时为 `null`，开启时为按日期升序数组，每项含 `date` / `requests` / 输入输出与缓存 token / `cost` / `credits` / `saved` / `unpriced_requests`；`daily_models` 为 `usage_log.query()` 的内部返回字段，由 API 层计价消费后不对外暴露。

## 风险 / 权衡

- **全量内存**：10000 条 × ~300B ≈ 3MB，可接受；运行期滚动阈值兜底（超阈值保留较新一半，重写在写锁内完成）。
- **单价表由用户维护**：价格失真风险由「未配置显示 –」与配置页校验缓解。
- **历史日志无费用字段**：实时按当前单价表计算，历史与当前口径一致（价格变动后历史费用会随之变化），已在 proposal 记录。
- **缓存 token 为本地模拟**：`cache_read_tokens` 仅 Anthropic 端点由 `cache_sim.py` 注入，OpenAI 端点恒 0；费用与节省额是估算值，不代表上游真实计费。
- **8 列 vs 现有 11 列**：端点/首字耗时不再独立成列，信息密度下降；通过模型列副标签与「用时」保留主要信息。
- **滚动后累计数下降**：超阈值裁剪较旧一半，历史累计与按模型统计随之下降；单机代理场景可接受。
- **单价表解析开销**：每次查询解析一次并复用，10000 条规模下开销可忽略；非法配置每查询最多一条警告。

## 迁移方案

无需数据迁移。历史 `usage-log.jsonl` 直接兼容：新增的 `cache_creation_tokens` 缺失时按 0 计价，其余费用字段均为查询期计算。三个配置键缺省时按默认值工作（账号「主账号」、单价表空表、系数 1.0）。

## 待决问题

无。
