# usage-log Specification

## Purpose
记录代理双端点（OpenAI / Anthropic Messages）的每次成功聊天补全请求的用量与耗时，向 admin 管理界面提供持久化的使用统计与请求明细查询能力。

## Requirements

### Requirement: 请求用量记录
系统 SHALL 在 OpenAI `/v1/chat/completions` 与 Anthropic `/v1/messages` 端点的每次成功响应完成后，记录一条包含时间戳、来源端点、客户端 IP、模型名、是否流式、输入/输出/缓存读取 tokens、首字耗时与总耗时的日志记录；记录失败 SHALL NOT 影响代理主链路。

#### Scenario: 流式请求记录
- **WHEN** 客户端经任一端点发起流式聊天请求且上游正常返回
- **THEN** 响应结束后产生一条 `stream=true` 的记录，含首字耗时与总耗时
- **AND** 记录发生在最后一个 chunk 发送之后，不增加首字延迟

#### Scenario: 记录失败不影响主链路
- **GIVEN** 磁盘写入失败
- **WHEN** 某次请求完成并尝试记录
- **THEN** 代理仅打印警告日志，客户端响应不受影响

### Requirement: 日志持久化与滚动
系统 SHALL 将日志以 JSONL 追加写入本地文件并跨重启保留；文件超过条数阈值时 SHALL 清理最旧记录；记录 SHALL 仅含上述用量字段，不含消息内容或密钥。

#### Scenario: 重启后统计保留
- **GIVEN** 代理已记录若干请求后重启
- **WHEN** admin 查询使用统计
- **THEN** 历史记录与统计仍可查询

#### Scenario: 滚动清理
- **GIVEN** 日志文件超过设定条数阈值
- **WHEN** 代理启动
- **THEN** 最旧的记录被清理，文件规模受控

### Requirement: 使用统计查询 API
系统 SHALL 提供 admin 鉴权保护的使用统计接口，返回过滤后全量的 `summary` / `by_model`、当前页 `entries`（含 `account` / `cost` / `credits` / `saved`）、分页信息 `page`、本页聚合 `page_totals`（`requests` / `priced_requests` / `cost` / `credits` / `saved`，全部未计价时金额为空值）与可选的 `daily` 按天聚合；接口 SHALL 支持 `start` / `end` 日期区间过滤，明细查询 SHALL 覆盖日志文件内的全部记录（受运行期文件滚动阈值约束）；未认证访问 SHALL 返回 401。`summary` / `by_model` / `daily` 的 token 累计口径 SHALL 包含 `reasoning_tokens`（思考 token，为 `output_tokens` 的子集）。

#### Scenario: 认证查询
- **WHEN** 已登录 admin 会话请求使用统计接口
- **THEN** 返回汇总统计、按模型分组数据与分页明细
- **AND** 明细含时间、IP、账号、模型、流式与 effort 标签（effort 可空）、token 用量（输入、输出、缓存读取、缓存写入、思考；输入总计=输入+缓存读取；思考为输出子集，缺失时不展示数值）、费用、Credits、节省额与耗时字段
- **AND** 分页默认每页 100 条、上限 200 条
- **AND** 顶层返回 `page`（page/limit/total）与 `page_totals`（本页只累加有单价记录）

#### Scenario: 未认证拒绝
- **WHEN** 未携带有效 admin 会话请求该接口
- **THEN** 返回 401

#### Scenario: 日期区间过滤
- **WHEN** 请求携带 `start` 与/或 `end`（严格格式 `YYYY-MM-DD`，含首尾）
- **THEN** 仅返回该区间内的记录，汇总、按模型分组、分页总数与每日统计均按过滤后集合计算
- **AND** 参数格式非法（含非补零的 `2026-1-1`）或 `start` 晚于 `end` 时返回 400

#### Scenario: 每日统计
- **WHEN** 请求携带 `daily=true`
- **THEN** 额外返回按日期升序分组的聚合列表（日期、请求数、输入/输出/缓存读取/缓存写入/思考 tokens、费用、Credits、节省额、未计价条数）
- **AND** 日期按记录时间戳的自然日分组
- **AND** 聚合基于过滤后的全量记录，而非当前页
- **AND** 区间内无记录的日期不补零
- **WHEN** `daily` 缺省、为 `false` 或 `0`
- **THEN** 响应中 `daily` 为 `null`

#### Scenario: 全量明细
- **GIVEN** 日志文件中的记录数超过 200 条
- **WHEN** 分页查询使用日志
- **THEN** 可翻页访问全部记录，不受内存条数上限截断
- **AND** 运行期记录数超过文件滚动阈值时，内存明细与文件同步裁剪为较新一半
- **AND** 滚动后 `summary` / `by_model` 基于保留集合重建，累计数随之下降
- **AND** 查询返回的明细为副本，API 层注入账号与费用字段不写回日志文件

#### Scenario: 账号标识注入
- **GIVEN** 管理员配置了账号显示名
- **WHEN** 查询使用日志明细
- **THEN** 每条记录返回该账号名
- **AND** 未配置时返回默认「主账号」
### Requirement: 费用与 Credits 计算
系统 SHALL 依据模型单价表（键为上游模型 id）计算每条使用日志记录的费用、Credits 与缓存节省额；单价表 SHALL 由两部分合并而成：管理员可配置的 `CODEARTS_PRICING` 优先，其未覆盖的模型回退到随模型刷新自动拉取的官方单价表（来自 `GET {snap}/v1/model/builtin` 的 `credit` 字段）；两部分均未覆盖的模型 SHALL 返回空值而非零值，系统 SHALL NOT 猜测或内置默认价格。单价表条目 SHALL 至少包含输入与输出单价，缓存读取与缓存写入单价缺省时 SHALL 按输入单价计算。官方单价含多个档位时 SHALL 取 `time_slot_id` 为 null 或 `default` 的档位（多档匹配取首个），无匹配档位时该模型 SHALL 视为无官方单价。官方单价与使用日志的模型 id SHALL 精确匹配（区分大小写）。模型列表整体刷新失败时系统 SHALL 沿用既有缓存（含其中的官方单价），不视为空表。

#### Scenario: 按单价表计算费用
- **GIVEN** 单价表中配置了某上游模型 id 的输入、输出与缓存读取单价（单位：每百万 tokens）
- **WHEN** 查询该模型的使用记录
- **THEN** 记录费用 = 输入 tokens × 输入单价 + 输出 tokens × 输出单价 + 缓存读取 tokens × 缓存读取单价 + 缓存写入 tokens × 缓存写入单价（均按每百万 tokens 折算）
- **AND** Credits = 费用 × 可配置换算系数

#### Scenario: 官方单价表自动生效
- **GIVEN** 管理员未配置 `CODEARTS_PRICING`，且模型刷新已成功拉取官方 `credit` 单价
- **WHEN** 查询该模型的使用记录
- **THEN** 记录按官方单价计算费用与 Credits，界面显示具体数值而非「–」
- **AND** 官方价 `input_price` / `output_price` / `cache_price` 分别作为输入、输出与缓存单价，缓存写入按 `cache_price` 计
- **AND** 官方 `ratio` 字段不参与计算
- **AND** 官方单价存在多个 `time_slot_id` 档位时取 null 或 `default` 档，无匹配档位视为未计价

#### Scenario: 配置覆盖官方单价
- **GIVEN** 某模型同时在 `CODEARTS_PRICING` 与官方单价表中存在
- **WHEN** 查询该模型的使用记录
- **THEN** 以 `CODEARTS_PRICING` 中的单价为准
- **AND** 仅在 `CODEARTS_PRICING` 中不存在的模型回退官方单价

#### Scenario: 官方单价拉取失败
- **GIVEN** 上游 `/v1/model/builtin` 请求失败或返回结构异常，且模型列表同步成功
- **WHEN** 模型列表刷新与后续使用日志查询
- **THEN** 模型列表同步不受影响
- **AND** 官方单价表视为空，未在 `CODEARTS_PRICING` 中配置的模型费用与 Credits 返回空值

#### Scenario: 模型刷新整体失败沿用缓存
- **GIVEN** 模型列表刷新整体失败（AgentCenter 不可达或返回异常）
- **WHEN** 后续使用日志查询
- **THEN** 系统沿用既有 `models-cache.json`（含其中已拉取的官方单价）
- **AND** 不将官方单价表视为空

#### Scenario: 缓存节省额
- **GIVEN** 记录包含缓存读取 tokens 且缓存读取单价低于输入单价
- **WHEN** 查询该记录
- **THEN** 返回缓存节省额 = 缓存读取 tokens ×（输入单价 − 缓存读取单价）
- **AND** 无缓存读取时缓存节省额为零
- **AND** 缓存读取单价高于输入单价时，节省额截断为零而非负数

#### Scenario: 未配置单价
- **GIVEN** 某模型既未出现在 `CODEARTS_PRICING` 中，也不在官方单价表中
- **WHEN** 查询其使用记录
- **THEN** 该记录的费用与 Credits 返回空值
- **AND** 界面以「–」展示，不显示 0

#### Scenario: 聚合中的未计价记录
- **GIVEN** 过滤后的记录集合中同时存在有单价与无单价的记录
- **WHEN** 查询汇总或每日统计
- **THEN** 费用、Credits 与节省额只累加有单价的记录
- **AND** 返回未计价记录条数供界面提示

#### Scenario: 运行期单价表非法
- **GIVEN** 配置文件中的单价表不是合法 JSON 或包含负数
- **WHEN** 服务读取配置
- **THEN** 系统将 `CODEARTS_PRICING` 按空表处理并打印警告，改用官方单价表计算
- **AND** 代理主链路不受影响

#### Scenario: 脏数据与异常兜底
- **GIVEN** 某条记录含非数值 token 字段或计价过程抛出异常
- **WHEN** 查询该记录
- **THEN** 该记录按未计价处理，费用与 Credits 返回空值
- **AND** 查询整体成功返回，不产生 500 错误

### Requirement: 思考强度采集口径
系统 SHALL 为每次成功请求记录一条思考强度（effort）字段，取值来源按优先级为请求体的 `output_config.effort` → `thinking.effort`；两者均无有效非空字符串时，该字段 SHALL 记为 `high`（与发送给上游的兜底值同口径）。当 `thinking` 为对象且 `type` 为 `disabled` 时，系统 SHALL 判定思考未开启并将 effort 记为 `null`。

#### Scenario: 客户端仅携带 output_config
- **GIVEN** Anthropic 请求体含 `output_config: {"effort": "max"}` 且无 `thinking.effort`
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `effort` 为 `"max"`

#### Scenario: 客户端携带 thinking.effort
- **GIVEN** Anthropic 请求体含 `thinking: {"type": "enabled", "effort": "low"}` 且无 `output_config`
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `effort` 为 `"low"`

#### Scenario: 两个来源同时存在
- **GIVEN** 请求体同时含 `output_config.effort` 与 `thinking.effort` 且取值不同
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 记录的 `effort` 取 `output_config.effort` 的值

#### Scenario: 思考显式关闭
- **GIVEN** 请求体含 `thinking: {"type": "disabled"}`，且含或不含 `output_config.effort`
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `effort` 为 `null`
- **AND** 转发给上游的请求体 SHALL NOT 包含 `reasoning_effort` 字段

#### Scenario: 未携带任何 effort
- **GIVEN** 请求体既无 `output_config` 也无 `thinking.effort`，且 `thinking` 未标记为 disabled
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `effort` 为 `"high"`
- **AND** 转发给上游的请求体 SHALL 包含 `reasoning_effort: "high"`

#### Scenario: 非法类型兜底
- **GIVEN** `output_config` 或 `thinking` 不是对象，或 `effort` 为空串或非字符串
- **WHEN** 系统解析 effort
- **THEN** 该来源 SHALL 被视为无有效值，继续尝试下一来源
- **AND** 解析过程 SHALL NOT 抛出异常或影响请求主链路

### Requirement: 思考强度透传与记录同源
系统 SHALL 使 Anthropic 端点转发给上游的 `reasoning_effort` 与写入使用日志的 `effort` 使用同一解析结果，以保证日志如实反映发送给上游的推理强度；解析结果为空时两端 SHALL 同时取兜底值 `high`（思考显式关闭的请求除外，该情形两端均为空）。

#### Scenario: 透传与记录一致
- **GIVEN** 客户端携带 `output_config.effort: "low"`
- **WHEN** 系统转发该请求并写入使用日志
- **THEN** 上游请求体 `reasoning_effort` 为 `"low"`
- **AND** 使用日志记录的 `effort` 为 `"low"`

#### Scenario: 兜底值两端一致
- **GIVEN** 客户端未携带任何 effort 且未关闭思考
- **WHEN** 系统转发该请求并写入使用日志
- **THEN** 上游请求体 `reasoning_effort` 为 `"high"`
- **AND** 使用日志记录的 `effort` 为 `"high"`
### Requirement: 思考 token 用量采集
系统 SHALL 在每次成功响应完成后，将本次请求的深度思考 token 数记入日志条目的 `reasoning_tokens` 字段：优先取上游 usage 的 `completion_tokens_details.reasoning_tokens`（整数、非布尔且非负时）；上游未提供时若响应携带非空思考文本（OpenAI 格式 `reasoning_content` 或等价思考增量），SHALL 按思考文本字符数除以 4 本地估算；两个来源的最终值 SHALL 统一截断为不超过 `output_tokens`（`output_tokens` 为 0 时记 0）；两者皆不可得时 SHALL 省略该字段。思考 token 属于输出 token 的子集，计价 SHALL NOT 因该字段重复计算。

#### Scenario: 上游细分字段优先
- **GIVEN** 上游 usage 含 `completion_tokens_details.reasoning_tokens: 120`，且该条记录 `output_tokens` 为 `300`
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `reasoning_tokens` 为 `120`（未超 `output_tokens`，不被截断）

#### Scenario: 思考文本估算兜底
- **GIVEN** 上游 usage 无 reasoning_tokens 细分，且响应含 800 字符思考文本，该条记录 `output_tokens` 为 `500`
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `reasoning_tokens` 为 `200`（800 ÷ 4，未超输出总数）

#### Scenario: 估算值截断
- **GIVEN** 上游 usage 无细分字段，思考文本估算值大于该条记录的 `output_tokens`
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `reasoning_tokens` 等于 `output_tokens`，SHALL NOT 大于输出 token 总数
- **AND** `output_tokens` 为 0 时 `reasoning_tokens` 为 0

#### Scenario: 上游细分脏数据截断
- **GIVEN** 上游 usage 的 `completion_tokens_details.reasoning_tokens` 大于该条记录的 `output_tokens`（上游脏数据）
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条记录的 `reasoning_tokens` 被截断为 `output_tokens`

#### Scenario: 上游细分非法值
- **GIVEN** 上游 usage 的 `completion_tokens_details.reasoning_tokens` 为负数、布尔或非整数类型
- **WHEN** 系统解析该字段
- **THEN** 该来源视为无值，继续按思考文本估算兜底处理

#### Scenario: 双来源皆无
- **GIVEN** 上游 usage 无细分字段且响应无思考文本
- **WHEN** 请求成功完成并写入使用日志
- **THEN** 该条日志条目 SHALL 省略 `reasoning_tokens` 键

#### Scenario: 双端点一致
- **GIVEN** 同一上游响应结构分别经 OpenAI 端点与 Anthropic 端点代理
- **WHEN** 两端各自写入使用日志
- **THEN** 两端日志条目的 `reasoning_tokens` 采集口径一致（优先级与截断规则相同）

### Requirement: 思考 token 统计聚合
系统 SHALL 将 `reasoning_tokens` 纳入使用统计的聚合口径：`summary`、`by_model` 与每日聚合 SHALL 累计该字段；历史日志条目缺失该字段时 SHALL 按 0 累计，SHALL NOT 使查询与聚合抛错。

#### Scenario: 聚合累计
- **GIVEN** 过滤后的记录集合中存在含 `reasoning_tokens` 的新记录与缺失该字段的历史记录
- **WHEN** 查询使用统计
- **THEN** `summary` / `by_model` / `daily` 的思考 token 合计只累加新记录的值，历史记录按 0 计
- **AND** 查询正常返回，无异常

### Requirement: 思考 token 前端展示
admin「使用日志」页 SHALL 在请求日志明细行展示思考 token 数：字段存在时显示数值，缺失时显示「–」；按模型分组表与每日统计表 SHALL 提供「思考」列，聚合行缺失该字段时按 0 展示（聚合计数语义）；「本页 Token」汇总卡片 SHALL 展示本页思考 token 小计。明细行与聚合表对缺失值的展示语义不同（明细行「–」表示未知，聚合表 0 表示无思考量），此为有意设计。

#### Scenario: 明细行展示
- **GIVEN** 使用日志页返回的明细中，一条记录含 `reasoning_tokens: 200`、另一条缺失该字段
- **WHEN** 渲染请求日志明细表
- **THEN** 前者 TOKEN 用量列展示「思考 Tokens: 200」，后者展示「思考 Tokens: –」
- **AND** 该行文案与既有的「思考强度」（effort）列语义区分（本处为 token 数量，非强度档位）

#### Scenario: 聚合表展示
- **GIVEN** 按模型分组或每日统计返回的聚合行含 `reasoning_tokens` 累计值
- **WHEN** 渲染对应表格
- **THEN** 「思考」列展示该值，缺失按 0 展示
- **AND** 表格空态提示行 SHALL 横跨新增列在内的全部列
