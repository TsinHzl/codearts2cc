## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: 使用统计查询 API
系统 SHALL 提供 admin 鉴权保护的使用统计接口，返回过滤后全量的 `summary` / `by_model`、当前页 `entries`（含 `account` / `cost` / `credits` / `saved`）、分页信息 `page`、本页聚合 `page_totals`（`requests` / `priced_requests` / `cost` / `credits` / `saved`，全部未计价时金额为空值）与可选的 `daily` 按天聚合；接口 SHALL 支持 `start` / `end` 日期区间过滤，明细查询 SHALL 覆盖日志文件内的全部记录（受运行期文件滚动阈值约束）；未认证访问 SHALL 返回 401。`summary` / `by_model` / `daily` 的 token 累计口径 SHALL 包含 `reasoning_tokens`（思考 token，为 `output_tokens` 的子集）。 ← (was 未含 reasoning_tokens 累计口径)

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