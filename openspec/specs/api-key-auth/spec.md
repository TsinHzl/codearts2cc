# api-key-auth Specification

## Purpose

为本地 CodeArts 代理提供本地 API Key 鉴权与自助管理能力：用户在 admin 管理页面生成 key，客户端以 `x-api-key` 或 `Authorization: Bearer` 携带该 key 访问 `/v1/*`；代理据此拒绝未授权调用，并在管理页面提供 key 的生成、启停与删除。

## Requirements

### Requirement: 请求鉴权门禁
The system SHALL 对 `/v1/*` 路径下的所有请求实施 API Key 鉴权，从 `x-api-key` 请求头或 `Authorization: Bearer <key>` 请求头提取 key（前者优先），并以恒定时间比较方式校验；`OPTIONS` 预检请求与 `/admin`、`/api/admin/*` 路径 SHALL 豁免鉴权。

#### Scenario: 携带有效 key 访问
- **GIVEN** 已存在至少一个启用状态的 API Key
- **WHEN** 请求 `/v1/chat/completions` 或 `/v1/messages` 且 `x-api-key` 头为有效 key
- **THEN** 系统放行请求并按原逻辑转发上游
- **AND** 响应内容与未引入鉴权前一致

#### Scenario: 通过 Bearer 头携带 key
- **GIVEN** 已存在启用状态的 API Key
- **WHEN** 请求 `/v1/*` 且以 `Authorization: Bearer <key>` 携带有效 key
- **THEN** 系统放行请求
- **AND** 当 `x-api-key` 与 `Authorization` 同时存在时，以 `x-api-key` 的值为准

#### Scenario: 缺少 key 被拒绝
- **GIVEN** 强制鉴权开启
- **WHEN** 请求 `/v1/*` 未携带任何 key
- **THEN** 系统返回 HTTP 401 且不转发上游
- **AND** 错误消息提示需在管理页面创建 API Key；当 key 列表为空时，消息 SHALL 明确说明"尚未创建任何 API Key"

#### Scenario: 浏览器跨域携带 key 可通过预检
- **GIVEN** 强制鉴权开启，请求来自回环源的浏览器工具
- **WHEN** 浏览器对 `/v1/*` 发起 `OPTIONS` 预检且 `Access-Control-Request-Headers` 含 `x-api-key`
- **THEN** 预检响应 SHALL 在 `Access-Control-Allow-Headers` 中列出 `x-api-key`
- **AND** 预检请求本身不被鉴权拒绝

#### Scenario: 无效 key 被拒绝
- **GIVEN** 强制鉴权开启
- **WHEN** 请求 `/v1/*` 携带的 key 未出现在 key 列表中
- **THEN** 系统返回 HTTP 401
- **AND** 错误消息不包含任何已存在的 key 明文

#### Scenario: 已禁用的 key 被拒绝
- **GIVEN** 某 API Key 处于禁用状态
- **WHEN** 请求携带该 key 访问 `/v1/*`
- **THEN** 系统返回 HTTP 403
- **AND** 错误消息说明该 key 已被禁用

#### Scenario: 关闭强制鉴权
- **GIVEN** 配置项 `CODEARTS_REQUIRE_API_KEY` 为 `0`
- **WHEN** 请求 `/v1/*` 未携带 key
- **THEN** 系统放行请求，行为与引入鉴权前一致

#### Scenario: 非 ASCII 或超长 key 不触发服务错误
- **GIVEN** 强制鉴权开启
- **WHEN** 请求 `/v1/*` 携带含非 ASCII 字节的 key，或携带远长于正常长度的 key
- **THEN** 系统返回 HTTP 401（或 403，若该值恰好匹配到禁用 key）
- **AND** 不因比较操作抛出异常而返回 500

#### Scenario: 管理面不受鉴权影响
- **WHEN** 请求 `/admin` 或 `/api/admin/*`
- **THEN** 系统不施加 API Key 校验
- **AND** 管理面仍按既有口令会话鉴权规则处理

#### Scenario: 管理面禁用时门禁自动放行
- **GIVEN** 未设置 `ADMIN_PASSWORD`，管理面整体禁用
- **WHEN** 请求 `/v1/*` 未携带 key，且强制鉴权处于开启状态
- **THEN** 系统放行请求
- **AND** 启动日志提示鉴权未生效及启用管理面的方式

#### Scenario: 错误协议按端点分流
- **WHEN** 鉴权失败的请求路径以 `/v1/messages` 开头
- **THEN** 响应体为 Anthropic 错误结构
- **AND** 其他 `/v1/*` 路径的响应体为 OpenAI 错误结构

### Requirement: API Key 管理接口
The system SHALL 在管理面提供 API Key 的增删改查接口，全部位于 `/api/admin/api-keys`，受既有口令会话鉴权保护；接口返回的 key 明文 SHALL 仅用于用户在管理页面查看与复制，不得写入日志。

#### Scenario: 查询 key 列表
- **GIVEN** 已登录管理会话
- **WHEN** 请求 `GET /api/admin/api-keys`
- **THEN** 返回全部 key 的 id、name、enabled、创建时间与 key 明文
- **AND** 响应同时返回当前强制鉴权开关状态

#### Scenario: 未登录访问被拒
- **WHEN** 未携带有效管理会话请求任一 `/api/admin/api-keys*` 接口
- **THEN** 系统返回 HTTP 401，与既有管理接口行为一致

#### Scenario: 生成新 key
- **GIVEN** 已登录管理会话
- **WHEN** 提交 `POST /api/admin/api-keys` 并给出名称
- **THEN** 系统生成一个新 key 并持久化，返回其完整明文
- **AND** 新 key 默认处于启用状态，且其 id 在当前列表中唯一

#### Scenario: 名称校验
- **WHEN** 提交的名称缺失、非字符串、为空或超过 64 字符
- **THEN** 系统返回 400 并说明原因，不创建 key

#### Scenario: 改名与启停
- **GIVEN** 已存在某 key
- **WHEN** 提交 `PATCH /api/admin/api-keys/<id>` 携带新的名称或 `enabled` 值
- **THEN** 系统更新对应字段并持久化
- **AND** 对不存在的 id 返回 404

#### Scenario: 删除 key
- **GIVEN** 已存在某 key
- **WHEN** 提交 `DELETE /api/admin/api-keys/<id>`
- **THEN** 该 key 从列表与存储文件中移除
- **AND** 使用该 key 的后续 `/v1/*` 请求返回 401；对不存在的 id 返回 404

### Requirement: API Key 存储与安全
The system SHALL 将 API Key 持久化到与 `config.json` 同目录的 `api-keys.json`，写入采用 tmp + os.replace 原子替换并将文件权限收紧为 0600；key 明文 SHALL NOT 出现在应用日志、异常信息或版本控制中。

#### Scenario: 持久化与权限
- **WHEN** 任一 key 被创建、修改或删除
- **THEN** 变更立即原子写入 `api-keys.json`
- **AND** 文件权限为 0600

#### Scenario: 重启后仍有效
- **GIVEN** 已创建若干 key
- **WHEN** 服务重启并再次接收请求
- **THEN** 此前创建的 key 仍然有效，无需重新生成

#### Scenario: 存储文件损坏
- **GIVEN** `api-keys.json` 内容不是合法 JSON 或结构非法
- **WHEN** 服务读取该文件
- **THEN** 系统按空列表处理并打印警告
- **AND** 鉴权门禁仍可正常工作，不因该文件损坏而崩溃

#### Scenario: 文件不存在
- **WHEN** 服务启动且 `api-keys.json` 不存在
- **THEN** 系统以空列表启动并不创建该文件
- **AND** 管理页面仍可正常打开与登录

### Requirement: API Key 管理页面
The system SHALL 在管理界面新增「API Key」页签，展示 key 列表并提供生成、改名、启停、删除与复制的操作入口；页面 SHALL 在未创建任何 key 时展示空状态并引导用户生成首个 key。

#### Scenario: 空状态引导
- **GIVEN** 尚无任何 API Key
- **WHEN** 用户打开 API Key 页签
- **THEN** 页面展示空状态提示与"生成 API Key"入口
- **AND** 页面同时对强制鉴权开关的当前状态给出说明

#### Scenario: 生成并展示明文
- **WHEN** 用户点击生成并填写名称后确认
- **THEN** 页面展示新生成的 key 明文，并提供复制按钮
- **AND** 复制成功后给出可见反馈

#### Scenario: 列表操作
- **GIVEN** 列表中存在若干 key
- **WHEN** 用户执行改名、启用/禁用或删除
- **THEN** 页面即时更新列表并反映持久化结果
- **AND** 删除操作在提交前要求用户确认

#### Scenario: 移动端可达
- **WHEN** 用户在移动端视口使用管理界面
- **THEN** API Key 页签可通过底部导航的「更多」抽屉进入
- **AND** 页面在窄视口下无横向溢出

### Requirement: 强制鉴权开关
The system SHALL 提供配置项 `CODEARTS_REQUIRE_API_KEY`，默认值为开启，取值仅接受 `0` 或 `1`；该配置 SHALL 可在管理配置页编辑并热生效，无需重启服务。

#### Scenario: 默认开启
- **WHEN** `config.json` 未配置 `CODEARTS_REQUIRE_API_KEY`
- **THEN** 系统按开启处理，`/v1/*` 请求要求有效 key

#### Scenario: 配置页热切换
- **GIVEN** 已登录管理会话且强制鉴权处于开启状态
- **WHEN** 用户在配置页将 `CODEARTS_REQUIRE_API_KEY` 置为 `0` 并提交
- **THEN** 后续 `/v1/*` 请求不再校验 key，无需重启服务

#### Scenario: 拒绝非法取值
- **WHEN** 用户提交 `CODEARTS_REQUIRE_API_KEY` 的值不是 `0` 或 `1`
- **THEN** 系统拒绝保存并返回可读错误信息
- **AND** `config.json` 中该键保持原值不被覆盖
