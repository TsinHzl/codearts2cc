# 变更提案：redesign-usage-log-page

## 背景

现有「使用日志」页（变更 `2026-10-17-add-usage-log-page`）提供 4 个 token 统计卡、按模型分组表与 11 列请求明细，但缺少按时间区间筛选、费用与 Credits 展示、账号列与每日统计能力，与用户提供的 kiro2ccproxy 使用日志页截图存在明显差距。

用户已明确要求新增截图所示的使用日志页面，并在阶段 0 澄清中确认三项方案：费用按可配置单价表计算、账号名来自配置且不查询 IP 归属地、明细保留全量与区间筛选及每日统计。

需要显式记录一处决策反转：归档提案 `2026-10-17-add-usage-log-page` 曾写明「无费用 / Credits 列（上游不返回计费信息，已与用户确认去掉）」。本次用户重新要求费用与 Credits 列，方案改为本地按可配置单价表计算，原决策作废。

需要显式记录两处计量口径限制：

- `cache_read_tokens` 来自 `cache_sim.py` 的本地模拟（仅 Anthropic 端点非 0、受 `CODEARTS_CACHE_SIM` 控制），并非上游真实计量；OpenAI 端点该字段恒为 0。因此费用、Credits 与缓存节省额均为按本地 token 计数的估算值。
- 现有 `anthropic_proxy.py` 记录的 `input_tokens` 已扣除 `cache_read` 与 `cache_creation`，但日志条目未保存 `cache_creation_tokens`；本次需在记录端补齐该字段，否则按 token 计价会系统性少算缓存写入部分。

## 目标范围

**在范围内：**
- `usage_log` 内存明细由最近 200 条改为全量；文件滚动由「仅启动时执行」改为「启动时 + 运行期按阈值触发」（超 10000 条保留较新一半，原子重写；阶段 0 已确认接受该上限，长期保留需外置存储，不在本次范围）
- 记录端新增 `cache_creation_tokens` 字段：Anthropic 端点取 cache 模拟写入量，OpenAI 端点显式写 0（不读取上游字段）
- `usage_log.query()` 新增 `start` / `end` 日期区间过滤与 `daily` 按天聚合
- 新增配置键 `CODEARTS_ACCOUNT_NAME`（默认「主账号」）、`CODEARTS_PRICING`（按**上游模型 id** 配置 input / output / cache_read / cache_creation 单价表）、`CODEARTS_CREDITS_RATE`（费用到 Credits 的换算系数，默认 1.0）
- 后端按 token 与单价表计算每条记录的 `cost` / `credits` / 缓存节省额；无单价配置的模型返回 `null`，前端显示「–」
- `/api/admin/usage` 新增 `start` / `end` / `daily` 参数，响应新增 `account`、每条明细的 `cost` / `credits` / `saved`（未计价为 null）、`page_totals` 本页聚合与 `daily` 按日聚合（`daily` 为假时为 null）
- 前端「每日统计」切换后以日聚合表替换请求日志表（列：日期 / 请求数 / 输入 / 输出 / 缓存读取 / 缓存写入 / 费用 / Credits / 节省 / 未计价条数），不补零、不分页；切回明细保留区间并重置页码为 1
- 保留现有「按模型分组」表，改为请求日志上方的可折叠面板（默认折叠）
- 前端 `UsageTab` 按截图重构：页头固定面包屑「主要 / 使用日志」、标题「使用日志」、副标题「按时间维度查看用量与请求明细」；时间范围预设（今日 / 7 天 / 14 天 / 30 天）+ 自定义区间 +「每日统计」切换 + 刷新；四张统计卡、请求日志 8 列表格、分页（每页 100 条）
- `ConfigTab` 新增上述三个配置项的编辑入口，`EDITABLE_KEYS` 同步扩展
- `README.md` 配置表与 `config.example.json` 同步新增三个配置键
- 补充 `test_pricing.py` / `test_usage_log.py` / `test_admin_ui.py` / `test_anthropic_proxy.py` 用例，重建 `admin-ui/dist/`

**不在范围内：**
- IP 归属地查询（需引入离线地理库，用户已确认不做）
- 失败请求记录（仍只记录成功响应）
- 日志导出、图表可视化、多账号体系
- 上游计费字段对接（CodeArts 响应不返回计费信息）

## 技术方案

- **费用计算**：`cost = input_tokens/1e6 × input_price + output_tokens/1e6 × output_price + cache_read_tokens/1e6 × cache_read_price + cache_creation_tokens/1e6 × cache_creation_price`，单价单位为 USD / 1M tokens；`CODEARTS_PRICING` 为 JSON 字符串，形如 `{"glm-5.3-flash": {"input": 0.1, "output": 0.3, "cache_read": 0.02, "cache_creation": 0.1}}`。
- **单价表键**：键为 `resolve_model()` 之后的上游模型 id（如 `glm-5.3-flash`、`openpangu-2.0-pro`），不是客户端 Claude 别名。`input` / `output` 必填，`cache_read` / `cache_creation` 缺省时按 `input` 单价计算。
- **Credits**：`credits = cost × CODEARTS_CREDITS_RATE`（默认 1.0）。
- **缓存节省额**：`saved = max(0, cache_read_tokens/1e6 × (input_price − cache_read_price))`，负值截断为 0；无缓存读取时显示「本页无缓存节省额」。
- **账号列**：单账号代理，`account` 不入日志文件，查询时由 `config_store.get("CODEARTS_ACCOUNT_NAME")` 统一注入，避免历史日志兼容问题。
- **时间区间**：`start` / `end` 必须先通过正则 `^\d{4}-\d{2}-\d{2}$` 与 `datetime.strptime` 双重校验（拒绝 `2026-1-1` 这类非补零格式），非法或 `start > end` 返回 400；合法值按 `entry.ts[:10]` 字符串前缀比较（含首尾），与本地时间口径一致。
- **聚合口径**：日期过滤在 `query()` 内先作用于全量明细，`summary`、`by_model`、分页 `total` 与 `daily` 全部基于过滤后的集合计算，`daily` 不是基于当前页。
- **API 响应契约**：`/api/admin/usage` 返回 `{summary, by_model, entries, page, page_totals, daily, account, start, end}`。`summary` / `by_model` 基于过滤后全量；`entries` 为当前页且每条含 `cost` / `credits` / `saved`（无单价为 `null`）；`page` = `{page, limit, total}`；`page_totals` = `{requests, priced_requests, cost, credits, saved}`（只累加有单价记录，`priced_requests` 为均价分母，本页全部未计价时 `cost` / `credits` / `saved` 为 `null`）；`daily` 为假时显式返回 `null`。
- **职责边界**：`usage_log.query()` 只负责存储、过滤、分页与原始 token 聚合，不导入 `pricing`、不读 `config_store`；`admin_ui.admin_usage()` 负责读取账号名、解析单价表、为每条明细与每日统计补齐费用字段，并用 `{**entry, ...}` 构造新对象，绝不修改 `_entries` 中的元素。`usage_log.query()` 返回的 `entries` 本身也须为 `dict(entry)` 副本，避免派生字段被运行期滚动写回文件。
- **每日统计原始聚合**：`usage_log.query(daily=True)` 返回的每项只含 `date` / `requests` / `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens`，费用字段由 API 层补齐后返回。
- **滚动口径**：`record()` 先追加写入文件与 `_entries`、`_file_lines += 1`，再判断是否超过 `_MAX_FILE_ENTRIES`；超阈值时在写锁内保留较新一半、原子重写文件、重置 `_file_lines`，并基于保留集合同步重建 `_summary` 与 `_by_model`。`_file_lines` 统计成功解析的条目数，不是物理行数。滚动后累计总数会下降，属预期行为。
- **数值校验**：单价数值与 Credits 系数必须是有限实数（`math.isfinite`）且非负，布尔值视为非法；`CODEARTS_CREDITS_RATE` 解析失败或非法时回退 1.0。
- **单价表解析频率**：每次 API 查询解析一次单价表并复用结果，不在每条记录上重复 `json.loads`；非法配置每次查询最多打印一条警告。
- **脏数据兜底**：单条记录计价遇到非数值 token 或异常时按未计价处理（`null`），不中断查询、不返回 500。
- **展示口径**：本页 Token = 输入 + 输出 + 缓存读取 + 缓存写入；环形占比 = 缓存读取 ÷ 本页输入总计（输入 + 缓存读取），保留一位小数，总量为 0 时显示 0%（该口径与截图 84.8% 一致）；明细「输入总计」= 输入 + 缓存读取（对齐截图，缓存写入单独展示）；时间列固定 `YYYY/MM/DD HH:mm:ss`；「用时」列取 `duration_ms`，无值显示「–」，`first_token_ms` 作为悬浮提示；费用固定 `$` 前缀 + 4 位小数，Credits 固定 4 位小数并带「积分」单位；行内「省 x」与 Credits 卡节省额统一以积分（费用 × Credits 系数）展示；本页存在未计价记录时在费用卡副行提示「另有 N 条未计价」。
- **`daily` 参数**：`true` 或 `1`（大小写不敏感）视为开启，其余取值（含缺省、`false`、`0`）视为关闭并返回 `null`；区间内无记录的日期不补零。
- **统计卡口径**：总请求数取区间总数，副行固定「当页 {n} / 共 {total} 条 | 第 {page} / {pages} 页」；本页费用副行为「单请求均价 $x.xxxx」；本页 Credits 副行为「本页缓存节省额 x.xxxx 积分」或「本页无缓存节省额」；本页 Token 副行为「缓存读取 {n} {pct}%」并附环形进度；均价分母为「有单价记录数」；本页全部无单价时费用与 Credits 显示「–」。
- **展示精度**：费用与 Credits 固定 4 位小数（与截图一致），token 千分位；后端返回未舍入浮点值。
- **配置校验**：`CODEARTS_PRICING` 写入前校验 JSON 可解析、顶层为对象、每个值为对象、数值为有限非负实数（拒绝布尔值与 `NaN` / `Infinity`），且 `input` / `output` 存在；不通过返回 400 且不写盘。运行期解析失败回退空表并打印警告。前端提交前用 `JSON.stringify(JSON.parse(text))` 压缩为单行，满足现有 `isprintable()` 单行约束。
- **配置生效时机**：`pricing.py` 每次 API 查询解析一次 `config_store.get()` 的当前值，保存后下一条查询即按新单价计算；不引入跨请求缓存。

## 预期影响

- 内存：明细保留量从 200 条提升至文件内全量，运行期超过 10000 条触发滚动并同步裁剪内存（约数 MB 量级）。
- API：区间过滤与聚合在内存中完成，10000 条规模下开销可忽略；分页默认每页 100 条、上限 200 条。
- 兼容性：历史日志无费用字段，按当前单价表实时计算；账号名统一注入当前配置值。
- 数据兼容：历史日志条目无 `cache_creation_tokens`，计价时按 0 处理，不报错。
- 滚动行为：运行期触发滚动后，累计请求数、按模型分组与每日统计只反映保留的较新一半记录，总数会下降。
- 前端：`admin-ui/dist/` 需重建入库，运行 server 仍零 Node 依赖。

## 风险

- **单价表配置错误导致费用失真**：无配置或解析失败的模型显示「–」，配置页提供格式示例，不猜测价格。
- **全量明细内存增长**：运行期也按 `_MAX_FILE_ENTRIES = 10000` 触发滚动，超阈值保留较新一半；滚动期间持有写锁，单次重写 5000 行，耗时在毫秒级。
- **时区口径**：`entry.ts` 为本地时间 `isoformat`，按字符串前缀比较天然一致；跨时区客户端不影响服务端本地口径。
- **历史账号名不一致**：历史记录显示当前配置账号名；单账号场景可接受，如需追溯需后续引入账号字段。
- **缓存指标为估算值**：`cache_read_tokens` 由本地模拟产生，费用与节省额仅用于相对参考，不代表上游实际计费。
- **历史费用随单价表变动**：费用是查询期实时计算，修改单价表会同时改变历史记录展示值。
- **滚动后累计数下降**：超 10000 条裁剪较旧一半，历史累计与按模型统计随之下降；单机代理场景可接受，若需长期保留应改用外部存储。
