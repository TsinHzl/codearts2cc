# 变更提案：add-oauth-login

## 背景

当前 codearts2cc 依赖 `config.json` 中的长期固定 AK/SK（`CODEARTS_AK` / `CODEARTS_SK`）调用华为云 CodeArts 上游，
缺失时 `server.py` 与 `benefit.py` 在导入/启动阶段直接 `SystemExit`。固定 AK/SK 无有效期、需用户手工申请与轮换，
泄露后影响面大，且与 codearts2api 已有的 OAuth2 浏览器登录体验不一致。

本变更把上游认证方式整体替换为 codearts2api 采用的「OAuth2 PKCE + DPoP 浏览器登录 → STS 临时凭据 + refresh_token
自动续期」，同时保留现有管理端口令鉴权与 `/v1/*` 本地 API Key 鉴权不变。

## 目标范围

**在范围内：**

- 新增 OAuth2 PKCE + DPoP 登录流程：生成授权链接、接收回调、`ticket` 兜底轮询、授权码换取 STS 临时凭据。
- 单账号 OAuth 凭据落盘（0600、tmp + `os.replace` 原子写）：`access_key_id`、`secret_access_key`、
  `security_token`、`expiration`（RFC3339 UTC）、`refresh_token`、`code_verifier`、`client_id`、`dpop_private_key`、
  `user_id`、`user_name`、`domain_id`、`updated_at`。
- 后台续期线程：到期前 30 分钟用 refresh_token 自动刷新（已过期同样尝试刷新）；refresh_token 终态失效时
  标记「需重新登录」，停止重试。
- 管理界面新增「登录」页签：展示登录状态、账号名、凭据到期时间，提供「登录」「导入回调 URL」「退出登录」；
  凭据刷新完全由后台自动完成，不提供手动刷新按钮。
- 管理端点：`POST /api/admin/oauth/start`、`GET /api/admin/oauth/poll`、`POST /api/admin/oauth/import-callback`、
  `POST /api/admin/oauth/logout`、`GET /api/admin/oauth/status`，挂载为嵌套蓝图并继承既有口令会话鉴权。
  `poll` 为短轮询（立即返回当前状态，前端每 1.5 秒请求一次）；登录会话总数上限 20，单个管理会话活跃登录
  上限 5，超出返回 409。
- 首次使用路径：设置 `ADMIN_PASSWORD` → 重启服务 → 浏览器打开 `/admin` 输入口令 → 切换到「登录」页签发起 OAuth。
  未设置 `ADMIN_PASSWORD` 时服务仍正常启动，`/admin` 页面壳与静态资源仍可打开，仅 `/api/admin/*` 返回 403，
  启动日志打印可操作的警告，聊天补全端点因无登录入口返回 503；**不采用「启动即退出」或「CLI 必填」**。
  `admin_ui._admin_gate` 相应调整为「未配置口令时页面放行、接口 403」，保持「管理页永远能打开」的既有原则。
- 上游签名改造：`_signed_request` 与 `_hmac_headers` 改为按当前 OAuth 账号取 STS 临时凭据并附加
  `X-Security-Token` 与 `X-Sdk-Content-Sha256`；`benefit.py` 同步改造，导入期不再因缺 AK/SK 退出。
- 日志脱敏：应用日志、异常信息与 werkzeug 访问日志均不得出现授权码、`secret`、`ticket_id`、`state`、
  `refresh_token`、STS 凭据明文、OAuth 流程涉及的 Cookie 值、上游请求体与上游响应体；异常信息只保留
  错误类型名与 HTTP 状态码。
- 无 OAuth 凭据时服务正常启动、管理页可打开并引导登录；`/v1/chat/completions` 与 `/v1/messages` 返回 503
  与可操作提示，`/v1/models` 与 `/v1/models/<id>` 回退 `models-cache.json` 或硬编码兜底列表。
- 配置页移除 `CODEARTS_AK` / `CODEARTS_SK` 的只读展示与启动强校验；`config.json` 中遗留的 AK/SK 不再被读取。
- `.gitignore` 增加 `oauth-auth.json` 与原子写临时文件（含 `/oauth-auth.json.tmp.*`）。
- 更新受影响的单元测试、`README.md`、`start.sh`、`config.example.json`、`CLAUDE.md` 与 `openspec/project.md`。

**不在范围内：**

- 多账号切换、账号列表与按请求选择账号。
- 管理端口令鉴权、登录失败限流、API Key 门禁与 key 管理的任何行为变化（例外：`_admin_gate` 仅调整
  「未配置口令时 `/admin` 页面放行、`/api/admin/*` 403」，口令校验本身不变）。
- 模型列表通道（agent / benefit）判定逻辑、SSE 修补与 Anthropic 协议转换逻辑。
- 模型别名映射、计价、使用日志等既有功能。
- 移植 codearts2api 的 CLI 登录工具（`cmd/login`）；登录只在管理页面完成。
- 手动刷新按钮与 `POST /api/admin/oauth/refresh`：续期由后台线程自动完成。
- 多进程部署：凭据刷新以单进程 `threaded=True` 为前提。
- `DPoP-Nonce` 挑战响应：参考实现同样未实现，本次不引入。
- 工作台 Cookie `devclouddevuibjJ_SESSION_ID`：本变更新增的 OAuth 登录、刷新与签名流程不读取、不转发、
  不记录该 Cookie；既有积分查询（`admin_ui._fetch_credit_usage()`）继续读取 `CODEARTS_SNAP_SESSION`
  并以该 cookie 名请求上游，行为不变。

## 技术方案

**新增 `oauth_login.py`（协议与凭据存储）**

- 常量对齐 codearts2api `DefaultLoginConfig`：`client_id=codearts-agent`、
  `portal=https://codearts.huaweicloud.com/portal`、
  `snap_manager=https://snap-access.cn-north-4.myhuaweicloud.com/snap-manager`、
  `sts=https://sts.cn-north-4.myhuaweicloud.com`、`redirect_path=/oauth/callback`、
  `plugin_name=snap_AIIDE`、`plugin_version=5.2.0`。
- PKCE：生成 64 字节随机数 → `base64url` 无填充得到 verifier；challenge = `base64url(sha256(verifier))`；
  授权链接使用 `code_challenge_method=SHA-256`。
- 授权链接参数固定为：`theme=2`、`locale=zh-cn`、`uri_scheme={client_id}`、`client_id`、`port`、
  `code_challenge`、`code_challenge_method=SHA-256`、`state`、`ticket_id`、`plugin-name`、`plugin-version`；
  `port` 统一取自 `LISTEN_PORT`（即 `CODEARTS_PROXY_PORT`）。
- 登录会话生成 `state = secrets.token_hex(16)` 并记录创建它的管理会话标识；`poll` / `import-callback` /
  `logout` 均校验请求来自同一管理会话，跨会话访问返回 409。不额外引入 CSRF token。
- DPoP：P-256 私钥（含 `d`）持久化在凭据文件中，每次换码/刷新生成 ES256 JWT（header 带公钥 JWK，
  payload 含 `htm` / `htu` / `iat` / `jti`，低 S 归一化）；`htu` 取完整 token URL
  （`https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`，无 query、无尾斜杠），`htm=POST`，
  `jti` 为 16 字节随机数 hex。不实现 `DPoP-Nonce` 挑战响应，`InvalidDPoPHeader` 判为终态失效。
- 换码：`POST {sts}/v1/oauth2/tokens`，`application/x-www-form-urlencoded`，表单含 `client_id`、`code`、
  `code_verifier`、`grant_type=authorization_code`、`redirect_uri=http://127.0.0.1:{port}/oauth/callback`，
  请求头带 `DPoP`。
- 刷新：同一端点、`grant_type=refresh_token`，表单含 `client_id`、`code_verifier`、`refresh_token`，
  复用签发时的 DPoP 私钥与 `client_id`；`code_verifier` 与 `client_id` 必须随凭据持久化，否则无法续期。
- `ticket` 兜底：`GET {snap_manager}/v1/login/ticket?ticket_id=&secret=`，**不参与签名**，仅带 `plugin-name` /
  `plugin-version` 请求头，不附加 STS 凭据头与 `X-Sdk-Content-Sha256`（此时尚未取得 STS 凭据，与 codearts2api
  的 PollTicket 一致）；响应同时兼容 `credentials` 与 legacy `credential` 两种字段结构，归一化后缺失凭据即
  报错。首次 secret 回调或 `import-callback` 带 secret 时置 `ticket_fallback=True`，此前轮询不请求 ticket；
  每次 poll 最多发起 1 次 ticket 请求。
- 双阶段回调：首次回调携带 `secret + redirect`（307 跳回并更新 pending secret），第二次只带 `code`；
  按「secret 命中 → ticket_id 命中 → 活跃会话按创建时间倒序（上限 10）」逐个尝试换码。
- 身份补全：优先 `GET {sts}/v5/caller-identity`，其次 `GET {snap_manager}/v1/current/user`，两者均使用
  同一 STS 凭据签名；均失败时解析 refresh_token JWT 的 `sub` 兜底，`sub` 仅用于展示。
- 凭据文件 `oauth-auth.json`（与 `config.json` 同目录，0600）：模块内锁 + 原子写；临时文件名
  `oauth-auth.json.tmp.<pid>.<随机 hex>` 保证唯一；明文只存该文件，不写日志、不进异常堆栈、不入 git。
- 对外接口：`init(path)`（测试可注入 `tmp_path`）、`current_credentials()` 返回不可变凭据快照、
  `refresh_if_needed()`、`credentials_status()`、`CredentialsUnavailable` 异常、
  `OAuthSessionStore`（15 分钟 TTL，纯内存）。

**新增 `admin_oauth.py`（管理端点）**

- 嵌套蓝图，继承 `admin_ui` 的 `before_request` 口令会话门禁，不重复挂载鉴权。
- `start` 生成 ticket_id / secret / PKCE / DPoP 与 `state`，并把登录会话绑定到当前管理会话；`poll` 为短轮询，
  立即返回当前状态（前端每 1.5 秒调用一次）；`import-callback` 接收用户粘贴的回调 URL；`logout` 删除凭据文件
  并停止续期；`status` 返回登录状态。
- 错误契约：未登录管理面 401；管理面禁用（未设置 `ADMIN_PASSWORD`）403；参数非法 400；会话不存在或不属于
  当前管理会话 409；换码失败 502；`/v1/*` 未登录 503。
- 所有响应不包含 `refresh_token`、`code_verifier`、`dpop_private_key`、STS 凭据明文。
- 登录入口依赖 `ADMIN_PASSWORD`：未设置时 `/admin` 页面壳与静态资源仍放行、仅 `/api/admin/*` 返回 403，
  `server.py` 启动时打印可操作警告；聊天补全端点因无登录入口返回 503；服务本身仍正常启动。

**改造 `server.py`**

- 删除启动期 `CODEARTS_AK` / `CODEARTS_SK` 强校验。
- 新增 `_current_credential()`：从 `oauth_login` 取 STS 快照，无凭据时抛 `CredentialsUnavailable`。
- `_signed_request` / `_hmac_headers` 改为使用该快照签名，附加 `X-Security-Token` 与
  `X-Sdk-Content-Sha256`（`sha256(body)` 的小写 hex，空 body 使用常量
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`）；函数签名不变，
  `anthropic_proxy.configure(hmac_headers=...)` 注入契约保持不变。
- 聊天补全 POST（`server.py`）、模型发现 GET（`server.py`）与 benefit（`benefit.py`）请求一律附加
  `X-Sdk-Content-Sha256` 并参与签名；ticket（`oauth_login.py`）请求不签名，仅带 `plugin-name` / `plugin-version` 头。
- 注册 `/oauth/callback` 路由（豁免管理口令，同时校验回环 Host 与回环 `remote_addr`），响应头设置
  `Cache-Control: no-store` 与 `Referrer-Policy: no-referrer`。
- 启动时立即执行一次 `refresh_if_needed()`（10 秒超时，失败不阻塞启动），随后在 `__main__` 中启动 daemon
  续期线程（30 分钟轮询、提前 30 分钟刷新）；用 `WERKZEUG_RUN_MAIN` 防止 Flask reloader 重复起线程。
- `/v1/chat/completions` 与 `/v1/messages` 捕获 `CredentialsUnavailable` → 503 + 提示前往管理页登录。
- `/api/admin/benefit`、`/api/admin/benefit/claim`、`/api/admin/credit` 在无凭据时返回可读错误而非 500。
- 安装 werkzeug logger filter，对访问日志 query string 中的 `code` / `secret` / `ticket_id` / `state` 值脱敏。

**改造 `benefit.py`**

- 移除导入期 AK/SK 强校验；`signed_request` 改用 `oauth_login.current_credentials()`，并附加
  `X-Security-Token` 与 `X-Sdk-Content-Sha256`。
- CLI 在无凭据时输出可读提示并以非零码退出（不影响 server 导入）。

**改造 `admin_ui.py` 与前端**

- `READONLY_SECRET_KEYS` 移除 `CODEARTS_AK` / `CODEARTS_SK`，配置页不再展示这两个键。
- 状态接口增加 OAuth 登录状态（是否已登录、账号名、到期时间、是否需重新登录）。
- `admin-ui/src` 新增「登录」页签并重新构建 `dist/`；登录状态轮询间隔 1.5 秒。

**文档与配置**

- `.gitignore` 增加 `/oauth-auth.json`、`/oauth-auth.json.tmp` 与 `/oauth-auth.json.tmp.*`。
- `README.md` 删除 AK/SK 获取说明，改为首次启动后在「登录」页签完成 OAuth 登录。
- `start.sh` 移除对 AK/SK 的提示，保留 `ADMIN_PASSWORD` 交互引导。
- `config.example.json` 移除 `CODEARTS_AK` / `CODEARTS_SK`。

## 预期影响

- 用户首次使用需在管理页完成一次浏览器登录；之后由 refresh_token 自动续期，无需再手工配置 AK/SK。
- 无凭据时服务不再拒绝启动，聊天补全端点（`/v1/chat/completions`、`/v1/messages`）返回 503，
  `/v1/models` 走缓存兜底；本地 API Key 鉴权行为不变。
- 已配置 `CODEARTS_AK` / `CODEARTS_SK` 的用户需重新登录一次，旧配置被忽略。
- `models-cache.json` 缓存机制不变；首次登录前模型列表回退到缓存或硬编码兜底列表。
- 既有测试中关于 `CODEARTS_AK` / `CODEARTS_SK` 的用例（`test_admin_ui.py`、`test_config_store.py`、
  `test_api_key_gate.py`）需要改为断言 OAuth 状态或注入假凭据；凭据目录通过 `oauth_login.init(tmp_path)` 隔离。
- `test_openai.py` 集成冒烟需先完成 OAuth 登录（或注入假凭据）后才能运行。
- 续期线程与凭据文件以单进程 `threaded=True` 为前提；不支持多 worker / 多进程部署。

## 风险

- **refresh_token 终态失效**：需用户重新登录。通过管理页显式提示与续期线程停止重试来避免无效轮询。
- **回调端口与绑定地址**：`redirect_uri` 必须与代理监听端口一致；仅在回环地址下开放 `/oauth/callback`，
  Host 与 `remote_addr` 任一非回环一律 403，避免被外部伪造回调；307 目标必须校验为 https、host 等于
  PortalHost、path 为 `/oauth/callback` 且不含 userinfo，防开放重定向；回调返回的 HTML 对参数文本做转义。
- **凭据落盘泄露**：文件权限 0600、原子写；响应与日志不输出 refresh_token / DPoP 私钥 / STS 明文；
  werkzeug 访问日志对 `code` / `secret` / `ticket_id` / `state` 值做脱敏。
  Windows 上 `os.chmod` 语义有限，按尽力而为处理并写入文档。
- **双阶段回调与多会话并发**：采用「secret → ticket_id → 活跃会话倒序」逐级匹配，授权码用错不消耗，
  但需限制候选数（10）与会话总数（20 / 5），超出返回 409，并记录不含敏感值的警告。
- **ticket 兜底缺少 refresh_token**：ticket 通道可能返回不含 refresh_token 的凭据，此时视为登录成功但
  不可自动续期，到期后需重新登录。
- **续期时序窗口**：30 分钟轮询 + 提前 30 分钟刷新，凭据有效期通常远大于轮询周期；已过期的凭据同样尝试
  刷新一次；启动时立即执行一次 `refresh_if_needed()`（10 秒超时，失败不阻塞）缩小重启后的空窗。
- **并发登录覆盖**：多个管理会话同时完成登录时后写覆盖前者；以 `updated_at` 较新者为准并记录一次提示。
- **多进程共享凭据文件**：两个进程同时刷新可能互相覆盖；写入前重新读取文件比较 `updated_at`，较旧者放弃写入。
  本次仅支持单进程部署。
- **opengw 兼容性**：已核对 codearts2api `models.go`，gateway/config 与 benefit/claim 均使用 STS 签名并附加
  `X-Security-Token` 与 `X-Sdk-Content-Sha256`，与 snap-access 一致；若上游行为变化，模型发现失败时仍回退缓存。
- **续期线程与 Flask 重载**：仅在 `__main__` 启动 daemon 线程，并用 `WERKZEUG_RUN_MAIN` 防止 reloader
  重复起线程；测试导入 `server` 不产生后台任务。
- **回滚方案**：切回旧版本代码并恢复 `config.json` 中的 `CODEARTS_AK` / `CODEARTS_SK` 即可；旧版本不会读取
  `oauth-auth.json`，该文件可保留也可删除。
