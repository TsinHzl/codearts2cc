## ADDED Requirements

### Requirement: 使用日志思考强度列
使用日志页的请求明细表 SHALL 提供独立的「思考强度」列，位置在「模型」列之后、「TOKEN 用量」列之前；该列 SHALL 展示该条记录的 `effort` 值，值为空时 SHALL 展示「–」。effort 值 SHALL NOT 同时在模型列下方重复展示。

#### Scenario: 有强度值的记录
- **GIVEN** 某条使用日志记录的 `effort` 为 `"high"`
- **WHEN** 用户查看请求明细表
- **THEN** 「思考强度」列显示 `high`
- **AND** 模型列下方不再显示 effort 文本

#### Scenario: 无强度值的记录
- **GIVEN** 某条记录来自旧日志数据，`effort` 缺失或为 `null`
- **WHEN** 用户查看请求明细表
- **THEN** 「思考强度」列显示「–」
- **AND** 其余列内容与排布不受影响

#### Scenario: 空结果提示
- **GIVEN** 当前区间无任何记录
- **WHEN** 明细表展示空态提示
- **THEN** 提示行 SHALL 横跨新增列在内的全部列
