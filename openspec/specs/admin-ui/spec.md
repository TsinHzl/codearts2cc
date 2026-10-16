# admin-ui Specification

## Purpose
为 codearts2cc 单账号本地代理提供内嵌管理界面，用于可视化管理模型列表、免费额度签到与运行时配置，并提供服务状态概览。登录采用口令鉴权与登录失败指数退避限流；配置值仅接受单行可打印字符串，按行精准写回 .env（tmp + os.replace 原子写）后立即生效。

## Requirements

### Requirement: 管理界面访问鉴权
The system SHALL 为管理界面提供口令鉴权：未配置 `ADMIN_PASSWORD` 环境变量时禁用管理界面；已配置时，未登录请求访问 `/api/admin/*` SHALL 返回 401，登录成功后 SHALL 授予会话访问权。

#### Scenario: 未配置口令时禁用
- **WHEN** 用户访问 `/admin` 且环境未设置 `ADMIN_PASSWORD`
- **THEN** 系统返回禁用提示页，不暴露任何模型、额度或配置数据
- **AND** `/api/admin/*` 接口拒绝访问

#### Scenario: 未登录访问受保护接口
- **GIVEN** 已设置 `ADMIN_PASSWORD`
- **WHEN** 未携带有效会话的请求访问任一 `/api/admin/*` 接口
- **THEN** 系统返回 HTTP 401
- **AND** 不返回任何管理数据

#### Scenario: 口令登录成功
- **GIVEN** 已设置 `ADMIN_PASSWORD`
- **WHEN** 请求携带正确的管理员口令登录
- **THEN** 系统建立会话并允许后续访问 `/api/admin/*`
- **AND** 错误口令 SHALL 被拒绝且不建立会话

#### Scenario: 登录失败限流
- **GIVEN** 已设置 `ADMIN_PASSWORD`
- **WHEN** 连续多次携带错误口令登录
- **THEN** 系统 SHALL 对登录尝试施加退避限流，防止暴力破解
- **AND** 会话凭据 Cookie SHALL 设置 HttpOnly 与 SameSite

### Requirement: 模型列表展示与刷新
The system SHALL 在管理界面展示当前账号可用模型，按 agent 与 benefit 双通道分组，并支持手动刷新。

#### Scenario: 展示双通道模型
- **WHEN** 已登录的管理员访问模型页
- **THEN** 系统展示全部模型及其 id、name、channel、context_window、max_tokens、supports_images 字段
- **AND** agent 通道与 benefit 通道分组区分显示

#### Scenario: 手动刷新
- **WHEN** 管理员触发手动刷新
- **THEN** 系统强制重新同步模型列表（绕过缓存 TTL 门控）
- **AND** 返回刷新后的模型列表

### Requirement: 免费额度与签到
The system SHALL 在管理界面展示免费额度余额（剩余 / 总配额 / 已用）与签到状态，并支持触发签到。

#### Scenario: 查询额度
- **WHEN** 已登录的管理员访问额度页
- **THEN** 系统展示剩余额度、总配额、已用额度与签到状态
- **AND** 上游查询失败时返回可读错误信息而非空数据

#### Scenario: 触发签到
- **WHEN** 管理员触发「立即签到」
- **THEN** 系统调用上游签到接口并返回最新签到状态与余额
- **AND** 签到失败时返回可读错误信息且不伪装为成功

### Requirement: 运行时配置编辑
The system SHALL 允许管理员编辑模型别名映射（`CODEARTS_ANTHROPIC_MODEL_MAP`）与缓存模拟开关（`CODEARTS_CACHE_SIM`），并 SHALL 将变更按行精准写回 `.env` 文件后立即生效。

#### Scenario: 编辑模型映射
- **WHEN** 管理员在配置页修改模型别名映射并保存
- **THEN** 系统按行精准更新 `.env` 中 `CODEARTS_ANTHROPIC_MODEL_MAP` 的值
- **AND** 进程内映射配置立即生效
- **AND** `.env` 中 AK/SK 行不被改写

#### Scenario: 切换缓存模拟开关
- **WHEN** 管理员切换 `CODEARTS_CACHE_SIM` 开关
- **THEN** 系统更新 `.env` 中该键的值并立即生效

#### Scenario: 密钥只读保护
- **WHEN** 管理员查看配置页
- **THEN** 系统仅以脱敏形式展示 `CODEARTS_AK`/`CODEARTS_SK`
- **AND** 这些密钥 SHALL 不可通过管理界面编辑

### Requirement: 服务概览
The system SHALL 在管理界面展示服务只读概览信息，包括监听地址、上游端点、模型数（agent/benefit 分组）、缓存开关态与模型缓存更新时间。