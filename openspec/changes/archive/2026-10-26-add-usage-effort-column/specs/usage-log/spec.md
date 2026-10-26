## ADDED Requirements

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
