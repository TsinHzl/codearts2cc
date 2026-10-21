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
系统 SHALL 提供 admin 鉴权保护的使用统计接口，返回过滤后全量的 `summary` / `by_model`、当前页 `entries`（含 `account` / `cost` / `credits` / `saved`）、分页信息 `page`、本页聚合 `page_totals`（`requests` / `priced_requests` / `cost` / `credits` / `saved`，全部未计价时金额为空值）与可选的 `daily` 按天聚合；接口 SHALL 支持 `start` / `end` 日期区间过滤，明细查询 SHALL 覆盖日志文件内的全部记录（受运行期文件滚动阈值约束）；未认证访问 SHALL 返回 401。

#### Scenario: 认证查询
- **WHEN** 已登录 admin 会话请求使用统计接口
- **THEN** 返回汇总统计、按模型分组数据与分页明细
- **AND** 明细含时间、IP、账号、模型、流式与 effort 标签（effort 可空）、token 用量（输入、输出、缓存读取、缓存写入；输入总计=输入+缓存读取）、费用、Credits、节省额与耗时字段
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
- **THEN** 额外返回按日期升序分组的聚合列表（日期、请求数、输入/输出/缓存读取/缓存写入 tokens、费用、Credits、节省额、未计价条数）
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
