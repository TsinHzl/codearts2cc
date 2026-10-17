## ADDED Requirements

### Requirement: 费用与 Credits 计算
系统 SHALL 依据管理员可配置的模型单价表（键为上游模型 id）计算每条使用日志记录的费用、Credits 与缓存节省额；未配置单价的模型 SHALL 返回空值而非零值，系统 SHALL NOT 猜测或内置默认价格。单价表条目 SHALL 至少包含输入与输出单价，缓存读取与缓存写入单价缺省时 SHALL 按输入单价计算。

#### Scenario: 按单价表计算费用
- **GIVEN** 单价表中配置了某上游模型 id 的输入、输出与缓存读取单价（单位：每百万 tokens）
- **WHEN** 查询该模型的使用记录
- **THEN** 记录费用 = 输入 tokens × 输入单价 + 输出 tokens × 输出单价 + 缓存读取 tokens × 缓存读取单价 + 缓存写入 tokens × 缓存写入单价（均按每百万 tokens 折算）
- **AND** Credits = 费用 × 可配置换算系数

#### Scenario: 缓存节省额
- **GIVEN** 记录包含缓存读取 tokens 且缓存读取单价低于输入单价
- **WHEN** 查询该记录
- **THEN** 返回缓存节省额 = 缓存读取 tokens ×（输入单价 − 缓存读取单价）
- **AND** 无缓存读取时缓存节省额为零
- **AND** 缓存读取单价高于输入单价时，节省额截断为零而非负数

#### Scenario: 未配置单价
- **GIVEN** 某模型未出现在单价表中
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
- **THEN** 系统按空单价表处理并打印警告，使用日志页费用显示「–」
- **AND** 代理主链路不受影响

#### Scenario: 脏数据与异常兜底
- **GIVEN** 某条记录含非数值 token 字段或计价过程抛出异常
- **WHEN** 查询该记录
- **THEN** 该记录按未计价处理，费用与 Credits 返回空值
- **AND** 查询整体成功返回，不产生 500 错误

## MODIFIED Requirements

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
