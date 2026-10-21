# usage-log Specification Delta

## MODIFIED Requirements

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
