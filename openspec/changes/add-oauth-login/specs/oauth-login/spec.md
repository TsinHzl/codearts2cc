## Purpose

为本地 CodeArts 代理提供华为云账号的 OAuth2 浏览器登录能力：用户在管理页面发起登录，代理以 PKCE + DPoP 换取 STS 临时凭据并持久化，随后用该临时凭据签名所有上游请求，并在凭据到期前自动续期。

## ADDED Requirements

### Requirement: OAuth 登录会话
The system SHALL 在管理面提供 OAuth 登录会话，生成包含 PKCE、DPoP 与 `state` 参数的授权链接，会话有效期 SHALL 为 15 分钟；会话 SHALL 绑定创建它的管理会话，登录会话总数上限为 20，同一管理会话的活跃登录上限为 5。

#### Scenario: 发起登录
- **GIVEN** 管理员已登录管理面
- **WHEN** 请求 `POST /api/admin/oauth/start`
- **THEN** 系统返回本次登录的 `session_id` 与可直接打开的授权链接
- **AND** 链接包含 `client_id`、`code_challenge`、`code_challenge_method=SHA-256`、`state`、`ticket_id`、`plugin-name`、`plugin-version` 与回环回调端口
- **AND** 回调端口取自代理监听端口 `LISTEN_PORT`

#### Scenario: 未登录管理面不可发起
- **WHEN** 未携带有效管理会话请求 `/api/admin/oauth/*`
- **THEN** 系统返回 HTTP 401，不生成任何会话

#### Scenario: 会话过期
- **GIVEN** 登录会话创建已超过 15 分钟且未完成授权
- **WHEN** 请求 `GET /api/admin/oauth/poll` 携带该 `session_id`
- **THEN** 系统返回过期状态
- **AND** 该会话不再参与后续回调匹配

#### Scenario: 短轮询
- **GIVEN** 登录会话仍在等待回调
- **WHEN** 前端以 1.5 秒间隔调用 `GET /api/admin/oauth/poll`
- **THEN** 系统立即返回当前状态，不阻塞等待
- **AND** 每次调用最多发起一次 ticket 请求

#### Scenario: 会话数量超限
- **WHEN** 活跃登录会话总数超过 20，或同一管理会话的活跃登录超过 5
- **THEN** `POST /api/admin/oauth/start` 返回 HTTP 409
- **AND** 不创建新会话

#### Scenario: 跨管理会话访问
- **GIVEN** 登录会话由管理会话 A 创建
- **WHEN** 管理会话 B 请求该会话的 poll、import-callback 或 logout
- **THEN** 系统返回 HTTP 409

#### Scenario: 响应不含敏感值
- **WHEN** 任一 `/api/admin/oauth/*` 接口返回结果
- **THEN** 响应体不包含 `refresh_token`、`code_verifier`、DPoP 私钥或 STS `secret_access_key` 明文

### Requirement: 授权回调处理
The system SHALL 在回环地址上接受 OAuth 授权回调，并支持浏览器不在代理主机时由用户手工导入回调 URL。

#### Scenario: 首次回调携带 secret
- **GIVEN** 存在活跃登录会话
- **WHEN** 回调请求携带 `secret` 与 `redirect` 参数
- **THEN** 系统定位匹配会话、更新其待用 secret 并标记 ticket 兜底可用，重定向回授权地址继续流程
- **AND** 不在此阶段换取凭据

#### Scenario: 第二次回调携带授权码
- **GIVEN** 存在活跃登录会话
- **WHEN** 回调请求携带 `code`
- **THEN** 系统按「secret 命中 → ticket_id 命中 → 活跃会话按创建时间倒序（上限 10）」依次尝试换取凭据
- **AND** 首次成功换取后立即结束该会话并持久化凭据

#### Scenario: 授权码与 PKCE 不匹配
- **WHEN** 某候选会话换码失败
- **THEN** 系统继续尝试其余候选会话
- **AND** 授权码不被消耗，可被后续候选继续使用

#### Scenario: 回调不来自回环地址
- **WHEN** `/oauth/callback` 请求的 Host 或 `remote_addr` 不是回环地址
- **THEN** 系统返回 HTTP 403，不处理任何参数

#### Scenario: 回调响应头与转义
- **WHEN** `/oauth/callback` 返回 HTML
- **THEN** 响应头包含 `Cache-Control: no-store` 与 `Referrer-Policy: no-referrer`
- **AND** 页面中的参数文本均经过 HTML 转义

#### Scenario: 手工导入回调 URL
- **GIVEN** 管理员已登录管理面
- **WHEN** 请求 `POST /api/admin/oauth/import-callback` 并提交完整的回调 URL
- **THEN** 系统按与浏览器回调相同的规则解析并尝试完成登录
- **AND** 缺少 `code`、`secret` 或格式非法时返回 400 与可读原因

#### Scenario: ticket 兜底登录
- **GIVEN** 登录会话已创建但未收到携带授权码的回调
- **WHEN** 轮询期间通过 ticket 通道取得登录结果
- **THEN** 系统完成登录并持久化凭据
- **AND** 该结果缺少 refresh_token 时仍视为登录成功但标记为不可自动续期
- **AND** 在首次 secret 回调或带 secret 的 import-callback 发生前，轮询不请求 ticket 接口

### Requirement: 凭据存储与安全
The system SHALL 将 OAuth 登录结果持久化到与 `config.json` 同目录的 `oauth-auth.json`，写入采用唯一临时文件名（`oauth-auth.json.tmp.<pid>.<随机 hex>`）+ `os.replace` 原子替换并将权限收紧为 0600；`expiration` SHALL 以 RFC3339 UTC 字符串存储；凭据明文 SHALL NOT 出现在应用日志、异常信息、管理面响应、访问日志或版本控制中；OAuth 流程涉及的 Cookie 值、上游请求体与上游响应体 SHALL NOT 写入任何应用日志或异常堆栈，异常信息 SHALL 只保留错误类型名与 HTTP 状态码；本变更新增流程 SHALL NOT 读取或使用工作台 Cookie `devclouddevuibjJ_SESSION_ID`，既有积分查询（`admin_ui._fetch_credit_usage()`）读取 `CODEARTS_SNAP_SESSION` 并以该 cookie 名请求上游的行为保持不变。

#### Scenario: 持久化与权限
- **WHEN** 一次登录成功换取凭据
- **THEN** 凭据立即原子写入 `oauth-auth.json`
- **AND** 文件权限为 0600

#### Scenario: 重启后保持登录
- **GIVEN** 已有未过期的 OAuth 凭据
- **WHEN** 服务重启
- **THEN** 系统直接复用该凭据，无需重新登录

#### Scenario: 存储文件损坏
- **GIVEN** `oauth-auth.json` 内容不是合法 JSON 或缺少必需字段
- **WHEN** 系统读取该文件
- **THEN** 按未登录处理并打印不含敏感值的警告
- **AND** 服务正常启动

#### Scenario: 退出登录
- **GIVEN** 已登录
- **WHEN** 请求 `POST /api/admin/oauth/logout`
- **THEN** 系统删除凭据文件、停止续期并使后续上游调用按未登录处理

#### Scenario: 访问日志脱敏
- **WHEN** werkzeug 记录 `/oauth/callback?code=...`、`?secret=...`、`?ticket_id=...` 或 `?state=...` 的访问日志
- **THEN** 日志中的参数值被替换为 `***`
- **AND** 应用日志与异常中不出现授权码、`refresh_token`、`code_verifier`、DPoP 私钥或 STS 凭据明文

#### Scenario: Cookie 与请求体不入日志
- **WHEN** OAuth 换码、刷新、ticket 轮询或身份补全出现网络异常、上游非 200 或 JSON 解析失败
- **THEN** 应用日志与异常堆栈只包含错误类型名与 HTTP 状态码
- **AND** 不包含任何 Cookie 值、上游请求体或上游响应体内容

#### Scenario: 新增流程不读取工作台 Cookie
- **WHEN** 执行 OAuth 登录、凭据刷新或上游签名
- **THEN** 系统不读取、不转发、不记录 `devclouddevuibjJ_SESSION_ID`
- **AND** 既有积分查询继续使用 `CODEARTS_SNAP_SESSION`，行为不变

### Requirement: 凭据自动续期
The system SHALL 在凭据到期前 30 分钟使用 refresh_token 自动刷新，已过期的凭据同样尝试刷新一次，并在 refresh_token 终态失效时停止重试并标记需重新登录。

#### Scenario: 到期前自动刷新
- **GIVEN** 已登录且凭据包含 refresh_token
- **WHEN** 距 `expiration` 不足 30 分钟
- **THEN** 系统自动调用刷新接口并原子写回新凭据
- **AND** 刷新复用签发时的 DPoP 私钥、`client_id` 与 `code_verifier`

#### Scenario: 凭据已过期
- **GIVEN** 凭据已超过 `expiration`
- **WHEN** 执行续期检查
- **THEN** 系统仍尝试使用 refresh_token 刷新一次
- **AND** 刷新成功则恢复可用状态

#### Scenario: 刷新失败可重试
- **WHEN** 刷新因网络错误或上游 5xx 失败
- **THEN** 系统保留原凭据并在 5 分钟退避后重试
- **AND** 不将状态标记为需重新登录

#### Scenario: refresh_token 终态失效
- **WHEN** 刷新返回 `invalid_grant`、`ExpiredRefreshToken`、`InvalidDPoPHeader` 或「has been used」类错误
- **THEN** 系统标记为需重新登录并停止自动刷新
- **AND** 管理面展示重新登录引导
- **AND** 后续上游调用按未登录返回 503

#### Scenario: 无 refresh_token 的登录
- **GIVEN** 本次登录仅取得 STS 凭据而未取得 refresh_token
- **WHEN** 凭据临近到期
- **THEN** 系统不发起刷新
- **AND** 到期后按需重新登录

### Requirement: 未登录时的降级行为
The system SHALL 在没有任何有效 OAuth 凭据时正常启动，管理面保持可访问，并对需要上游凭据的聊天补全请求返回可操作的错误；模型列表请求 SHALL 回退到本地缓存或兜底列表而非报错。

#### Scenario: 无凭据启动
- **GIVEN** `oauth-auth.json` 不存在
- **WHEN** 启动服务
- **THEN** 服务正常监听，`/admin` 可打开并展示未登录状态与登录入口

#### Scenario: 未设置管理口令
- **GIVEN** 未设置 `ADMIN_PASSWORD`
- **WHEN** 启动服务
- **THEN** 服务正常监听，`/admin` 页面壳与静态资源仍可访问
- **AND** `/api/admin/*` 返回 403，启动日志打印可操作警告
- **AND** 不因未登录或未设置口令而退出进程

#### Scenario: 无凭据调用聊天接口
- **WHEN** 未登录时请求 `/v1/chat/completions` 或 `/v1/messages`
- **THEN** 系统返回 HTTP 503
- **AND** 错误消息提示先在管理页面完成登录
- **AND** 该错误按端点协议分别以 OpenAI 或 Anthropic 错误结构返回

#### Scenario: 无凭据调用模型列表
- **WHEN** 未登录时请求 `/v1/models` 或 `/v1/models/<id>`
- **THEN** 系统不返回 503，而是返回 `models-cache.json` 缓存或硬编码兜底列表

#### Scenario: 无凭据调用管理额度接口
- **WHEN** 未登录时请求 `/api/admin/benefit`、`/api/admin/benefit/claim` 或 `/api/admin/credit`
- **THEN** 系统返回 503 与可读错误，不抛出 500

#### Scenario: 无凭据时模型刷新
- **WHEN** 未登录时触发模型列表刷新
- **THEN** 系统回退到 `models-cache.json` 或硬编码兜底列表
- **AND** 不因缺少凭据抛出未捕获异常

### Requirement: 上游请求使用 STS 临时凭据
The system SHALL 使用当前 OAuth 登录取得的 `access_key_id` / `secret_access_key` 进行 SDK-HMAC-SHA256 签名；STS 凭据必含 `security_token`，故实际始终附加 `X-Security-Token` 请求头（实现上仅在非空时附加），并始终附加取值 `sha256(body)` 小写 hex 的 `X-Sdk-Content-Sha256` 请求头（空 body 为 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`）；聊天补全、模型发现与额度签到/查询请求均 SHALL 使用该凭据来源，ticket 请求 SHALL NOT 签名。

#### Scenario: 聊天补全签名
- **GIVEN** 已登录且凭据有效
- **WHEN** 转发聊天补全请求
- **THEN** 请求以 STS 临时 AK/SK 签名
- **AND** 请求头包含 `X-Security-Token` 与 `X-Sdk-Content-Sha256`
- **AND** 参与签名的头与 `SignedHeaders` 保持一致

#### Scenario: 模型发现与额度接口签名
- **WHEN** 调用模型发现、签到或余额查询接口
- **THEN** 使用与聊天补全相同的 STS 凭据与 security token
- **AND** 请求头包含 `X-Sdk-Content-Sha256`

#### Scenario: ticket 请求不签名
- **WHEN** 轮询登录 ticket
- **THEN** 请求仅携带 `plugin-name` / `plugin-version` 头
- **AND** 不带 `Authorization`、`X-Security-Token` 或 `X-Sdk-Content-Sha256`

#### Scenario: 凭据在请求期间过期
- **WHEN** 上游以签名类错误拒绝请求且刷新后仍失败
- **THEN** 系统返回包含可读原因的上游错误
- **AND** 不在日志中输出任何凭据明文
