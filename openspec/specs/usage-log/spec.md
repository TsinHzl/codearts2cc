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
系统 SHALL 提供 admin 鉴权保护的使用统计接口，返回总请求数、输入/输出 tokens 汇总、按模型分组的统计，以及分页的请求明细列表；未认证访问 SHALL 返回 401。

#### Scenario: 认证查询
- **WHEN** 已登录 admin 会话请求使用统计接口
- **THEN** 返回汇总统计、按模型分组数据与分页明细
- **AND** 明细含时间、IP、端点、模型、流式与 effort 标签（effort 可空）、token 用量（输入、输出、缓存读取、输入总计=输入+缓存读取）与耗时字段

#### Scenario: 未认证拒绝
- **WHEN** 未携带有效 admin 会话请求该接口
- **THEN** 返回 401
