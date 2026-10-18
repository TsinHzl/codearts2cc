# 设计文档：add-oauth-login

## 上下文

当前 codearts2cc 的上游认证完全依赖 `config.json` 中的长期 AK/SK：

- `server.py:41-44` 在模块导入期校验 `CODEARTS_AK` / `CODEARTS_SK`，缺失即 `SystemExit`，导致服务无法启动、管理页不可访问。
- `benefit.py:36-39` 同样在导入期强校验，`server.py` 模块级 `import benefit` 会把该校验传染给主服务。
- `_signed_request`（`server.py:88-131`）与 `_hmac_headers`（`server.py:134-176`）只用全局 `AK` / `SK`，不含 `X-Security-Token`，也不设置 `X-Sdk-Content-Sha256`。

codearts2api 的方案是 OAuth2 授权码 + PKCE + DPoP：浏览器完成授权后，用授权码向华为 STS 换取一组临时凭据（`access_key_id` / `secret_access_key` / `security_token` / `expiration`）与 `refresh_token`，之后聊天补全、模型发现与额度接口的上游请求用该临时凭据做 SDK-HMAC-SHA256 签名（ticket 轮询发生在取得凭据之前，不签名），并在到期前用 `refresh_token` 自动续期。本设计将该方案移植到 Python 侧，同时保留现有管理端口令鉴权与 `/v1/*` 本地 API Key 鉴权。

## 目标 / 非目标

**目标**

- 无任何 OAuth 凭据时服务可启动，`/admin` 可打开并引导登录。
- 管理页完成一次浏览器授权后，代理持有 STS 临时凭据并可用于聊天补全、模型发现、签到与余额查询。
- 凭据到期前 30 分钟自动刷新，已过期的凭据同样尝试刷新一次；`refresh_token` 终态失效时停止重试并在管理页提示重新登录。
- 凭据仅落盘 `oauth-auth.json`（0600、原子写），不出现在日志、异常、管理面响应与版本控制中。
- 删除启动期 AK/SK 强校验与配置页对这两个键的只读展示。

**非目标**

- 多账号、账号切换与按请求选账号。
- 改变管理端口令鉴权、API Key 门禁、模型通道判定、SSE 修补与 Anthropic 协议转换。
- 移植 codearts2api 的 CLI 登录工具（`cmd/login`）。
- 引入数据库或密钥管理服务。
- `DPoP-Nonce` 挑战响应与多进程部署。

## 首次使用路径

1. 在 `config.json` 或环境变量中设置 `ADMIN_PASSWORD`。
2. 重启 `python3 server.py`；启动日志提示「尚未登录 CodeArts，请打开 /admin 完成登录」。
3. 浏览器打开 `http://127.0.0.1:51818/admin`，输入管理口令登录。
4. 切换到「登录」页签，点击「登录」获取授权链接并在浏览器中完成华为云授权。
5. 回调写入 `oauth-auth.json` 后，管理页显示已登录，`/v1/*` 立即可用。

未设置 `ADMIN_PASSWORD` 时服务仍正常启动，`/admin` 页面壳与静态资源仍可打开，仅 `/api/admin/*` 返回 403（`admin_ui._admin_gate` 相应调整：未配置口令时不拦截 `/admin` 与 `/admin/` 前缀，只拦截 `/api/admin*`），启动日志打印可操作警告，聊天补全端点（`/v1/chat/completions`、`/v1/messages`）返回 503，`/v1/models` 走缓存兜底。**不采用「未登录/未设口令即启动退出」或「CLI 必填」的方案**——管理页必须始终可打开，首次使用由用户在管理页自助完成。

## 决策

### D1：模块划分

- 新增 `oauth_login.py`：协议层（常量、PKCE、DPoP、授权链接、换码、刷新、ticket、身份补全）+ 凭据存储 + `OAuthSessionStore`。
- 新增 `admin_oauth.py`：管理端点蓝图，挂在 `admin_ui.blueprint` 下，继承既有口令会话门禁（与 `admin_api_keys.py` 同样的嵌套方式，不重复挂载鉴权）。
- `server.py` 只负责：签名取凭据、注册 `/oauth/callback`、启动续期线程、未登录降级。

理由：`server.py` 已超 1000 行，协议细节独立成模块便于单测；管理端点独立蓝图可复用现有 `before_request` 门禁。

凭据路径通过 `oauth_login.init(path)` 注入，生产环境由 `server.py` 在启动时以 `config.json` 同目录调用，测试使用 `tmp_path`。未调用 `init()` 前访问凭据相关接口抛 `RuntimeError`，避免测试之间共享真实凭据目录。

### D2：DPoP 使用 `cryptography>=42`

用 `ec.generate_private_key(ec.SECP256R1())` 生成密钥，签名流程：

1. `private_key.sign(input.encode(), ec.ECDSA(hashes.SHA256()))` 得到 DER。
2. `decode_dss_signature` 拆出 `(r, s)`。
3. `s > N/2` 时取 `N - s`（低 S 归一化，与 codearts2api 一致）。
4. `r` / `s` 各补零到 32 字节大端后拼接，`base64url` 无填充编码。

私钥 JWK 形如 `{"kty":"EC","crv":"P-256","x","y","d"}`，`x` / `y` / `d` 均为 32 字节大端 base64url。还原时校验 `0 < d < N`、点在曲线上、`d*G == (x, y)`。

JWT payload 的 `htu` 取完整 token URL：`https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens`（无 query、无尾斜杠），`htm` 为 `POST`，`jti` 为 16 字节随机数 hex。不实现 `DPoP-Nonce` 挑战响应（参考实现同样未实现）；刷新返回 `InvalidDPoPHeader` 时按终态失效处理。

理由：`cryptography` 是主流、有预编译 wheel、长期维护；`ecdsa` 纯 Python 实现性能与安全性都更弱。代价是新增一个二进制依赖，导入失败时 `oauth_login` 直接抛错并在消息中提示安装 `cryptography>=42`。

### D3：凭据快照不可变

`oauth_login.Credentials` 用 `@dataclass(frozen=True)`，字段：

`access_key_id`、`secret_access_key`、`security_token`、`expiration`（`datetime`，UTC 带时区）、`refresh_token`、`code_verifier`、`client_id`、`dpop_private_key`（`dict`，写入时深拷贝）、`user_id`、`user_name`、`domain_id`。

`code_verifier` 与 `client_id` 必须持久化：刷新请求的表单要求 `code_verifier` 与 `client_id`，且 DPoP 必须复用签发时的私钥，任何缺失都会导致续期失败。

`current_credentials()` 在锁内读取内存态并返回快照，调用方在整个请求生命周期内持有同一份快照，不受后台刷新竞态影响。

### D4：签名改造

`_signed_request` 与 `_hmac_headers` 改为：

1. 统一通过 `_current_credential()` 取快照；无凭据抛 `CredentialsUnavailable`。
2. 参与签名的头集合新增 `x-security-token`（`security_token` 非空时附加；STS 凭据必含该字段，故实际始终附加）与 `x-sdk-content-sha256`（`sha256(body).hexdigest()`）。
3. `x-sdk-content-sha256` 固定为小写 hex；空 body 为常量 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`；聊天补全 POST、模型发现 GET 与 benefit 请求一律附加并参与签名。ticket GET 发生在取得 STS 凭据之前，**不签名**，仅带 `plugin-name` / `plugin-version` 头。
4. `SignedHeaders` 由排序后的头名动态生成，因此新增头自动进入签名，无需硬编码。
5. `Authorization` 的 `Access=` 使用快照中的临时 `access_key_id`，HMAC key 使用临时 `secret_access_key`。

`_hmac_headers` 的 canonical URI 继续硬编码 `/api/v2/chat/completions/`（带尾斜杠），与现状一致。

测试使用固定向量断言两类签名头组合：聊天 POST + benefit 三头（含 `X-Security-Token`）与模型发现 GET，均校验 `X-Sdk-Content-Sha256` 出现在 `SignedHeaders` 中；另断言 ticket GET 仅含 `plugin-name` / `plugin-version`，且不产生 `Authorization` / `X-Security-Token` / `X-Sdk-Content-Sha256`。

### D5：凭据文件

文件名 `oauth-auth.json`，与 `config.json` 同目录。写入：`os.open(tmp, O_WRONLY|O_CREAT|O_EXCL, 0o600)` → 写入 → `os.replace`，临时文件名固定为 `oauth-auth.json.tmp.<pid>.<随机 hex>`，并发刷新不会互相覆盖。读取失败（JSON 非法、缺字段、JWK 校验失败）时按未登录处理并打印不含敏感值的警告，服务继续启动。

`expiration` 以 RFC3339 UTC 字符串落盘（如 `2026-10-18T12:00:00Z`），读取时解析为带时区 `datetime`。

刷新响应若不带新的 `refresh_token`，保留旧值；若带，则替换（华为的 refresh_token 可能一次性轮换）。写盘失败时保留内存中的新值并告警。退出登录时删除文件；删除失败时至少清空内存态并返回可读错误。

### D6：登录会话仓库

纯内存 `dict` + `threading.RLock`，会话字段：`id`、`ticket_id`、`secret`、`verifier`、`dpop_private_key`、`port`、`auth_url`、`state`、`admin_session`、`created_at`、`done`、`err`、`ticket_fallback`。

- TTL 15 分钟，每次读写前惰性 GC。
- 会话总数上限 20；同一管理会话的活跃登录上限 5；超出返回 409。
- `state = secrets.token_hex(16)`（128 bit），在 `start` 时生成并绑定当前管理会话标识（`admin_ui` 的 Flask session id）；`poll` / `import-callback` / `logout` 校验请求来自同一管理会话，跨会话访问返回 409。不额外引入 CSRF token。
- 候选匹配顺序：`secret` 命中 → `ticket_id` 命中 → 全部活跃会话按 `created_at` 倒序（上限 10 个）。
- 带 `secret` 或 `ticket_id` 但不命中：直接返回空，不再猜测。
- `ticket_fallback` 为假时轮询只返回 `pending`，不请求 ticket 接口，避免抢先保存一份不含 `refresh_token` 的凭据。
- 每次 poll 最多发起 1 次 ticket 请求，避免前端短轮询放大上游压力。

### D7：回调路由与双阶段处理

`/oauth/callback` 注册在 `server.app` 上而非管理蓝图，因为浏览器跳转不携带管理会话 cookie/令牌，且必须豁免口令门禁。

- Host 校验：解析 `request.host` 的主机名，`localhost` 或 `ipaddress.ip_address(...).is_loopback` 才处理；同时要求 `request.remote_addr` 为回环地址，任一不满足返回 403。
- 首次回调（带 `secret` + `redirect`）：用 `redirect` 里的 `ticket_id` 更新会话 secret、置 `ticket_fallback=True`，307 跳回 `redirect`，不换码。
- 307 目标校验：必须为 https、host 等于 `PortalHost`、path 为 `/oauth/callback`、不含 userinfo，否则拒绝。
- 第二次回调（带 `code`）：按 D6 候选顺序逐个调用换码；首个成功即结束会话并持久化；全部失败返回 502 与不含敏感值的提示；无候选返回 200 提示回管理页等待 ticket。
- 响应头设置 `Cache-Control: no-store` 与 `Referrer-Policy: no-referrer`；返回 HTML，便于用户直接在浏览器看到结果，所有参数文本均转义。

### D8：管理端点

`admin_oauth.py` 提供：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/admin/oauth/start` | 生成 ticket_id / secret / PKCE / DPoP / state，返回 `session_id` 与授权链接 |
| GET | `/api/admin/oauth/poll` | 短轮询，立即返回当前状态；`ticket_fallback` 为真时顺带轮询 ticket（每次最多 1 次） |
| POST | `/api/admin/oauth/import-callback` | 导入浏览器回调 URL（浏览器不在代理主机时） |
| POST | `/api/admin/oauth/logout` | 删除凭据文件、停止续期 |
| GET | `/api/admin/oauth/status` | 返回登录状态、账号名、到期时间、是否需重新登录 |

所有响应只返回状态与账号展示信息，不返回任何凭据字段。

`import-callback` 校验：`http`、无 userinfo、path 为 `/oauth/callback`、回环主机、端口等于会话端口；`redirect` 必须是 https 且 host 等于 PortalHost、`ticket_id` 与会话一致。

错误契约：

| 状态码 | 场景 | 响应 |
|---|---|---|
| 401 | 未携带有效管理会话 | `{"error": "unauthorized"}` |
| 403 | 管理面接口禁用（未设置 `ADMIN_PASSWORD`；`/admin` 页面与静态资源仍放行） | `{"error": "admin disabled"}` |
| 400 | 参数缺失或格式非法 | `{"error": "<可读原因>"}` |
| 409 | 会话不存在、已过期或不属于当前管理会话 | `{"error": "session not available"}` |
| 502 | 换码请求失败 | `{"error": "code exchange failed"}` |
| 503 | `/v1/*` 未登录 | OpenAI / Anthropic 各自错误信封 |

### D9：续期

- `refresh_if_needed()`：快照存在且有 `refresh_token` 时，若 `remaining <= 30min`（含 `remaining <= 0` 已过期）则刷新。
- 瞬时错误（网络错误、5xx）后进入 5 分钟退避，避免短轮询期间反复打上游。
- 启动时立即执行一次 `refresh_if_needed()`（10 秒超时，失败不阻塞启动），缩小服务重启后到首个轮询周期之间的空窗。
- 刷新前重新读取凭据文件比较 `updated_at`，若文件中的记录比内存态更新则先加载文件，避免多进程/多线程互相覆盖。
- 终态失效判定（仅 `grant_type=refresh_token`）：HTTP ≥ 400 且 `error == "invalid_grant"`，或 `error_code` 含 `ExpiredRefreshToken` / `InvalidDPoPHeader` / `invalid client id`，或小写 `error_msg` 含 `has been used` → 置 `needs_relogin`，停止重试；其余 4xx/5xx 与网络错误保留原凭据，下一轮重试。`needs_relogin` 为真时，`current_credentials()` 抛 `CredentialsUnavailable`。
- 后台线程：`daemon=True`，10 分钟轮询（提前量为 30 分钟，保证每个检查点距到期至少 20 分钟），仅在 `server.py` 的 `__main__` 中启动，并用 `WERKZEUG_RUN_MAIN` 判断防止 Flask reloader 重复起线程；导入模块不启动。

### D10：未登录降级

- `_current_credential()` 抛 `CredentialsUnavailable`。
- `/v1/chat/completions` 捕获后返回 503，OpenAI 错误信封：`{"error": {"message": "尚未登录 CodeArts，请先在管理页完成登录", "type": "service_unavailable", "code": "oauth_not_logged_in"}}`。
- `/v1/messages` 在 `anthropic_proxy` 内捕获，返回 `{"type": "error", "error": {"type": "api_error", "message": "尚未登录 CodeArts，请先在管理页完成登录"}}`（HTTP 503）。注意 `anthropic_proxy.py` 中 `signed_headers_fn(...)` 的调用位于 `try` 块之外，必须一并纳入捕获，否则会变成 500。
- `/api/admin/benefit`、`/api/admin/benefit/claim`、`/api/admin/credit` 在无凭据时返回 503 与可读消息，不再抛 500。
- 模型刷新捕获后回退 `models-cache.json` / 硬编码兜底列表。
- `/v1/models` 与 `/v1/models/<id>` 不返回 503：直接返回 `models-cache.json` 或硬编码兜底列表（与既有刷新回退链一致）。

### D11：配置与前端

- `admin_ui.READONLY_SECRET_KEYS` 移除 `CODEARTS_AK` / `CODEARTS_SK`（保留 `CODEARTS_SNAP_SESSION`）。
- `admin_ui.configure()` 新增 `oauth_status` 回调，供状态接口复用。
- 前端 `TabKey` 增加 `'login'`，`navDefs` 在「API Key」与「配置」之间插入「登录」；新增 `tabs/LoginTab.tsx`：状态卡（已登录 / 未登录 / 需重新登录）、登录按钮与授权链接、轮询（1.5 秒间隔短轮询）、回调 URL 导入框、退出登录。
- 重新构建 `admin-ui/dist/` 并提交。

### D12：依赖与部署

`requirements.txt` 增加 `cryptography>=42`。`cryptography` 在 macOS / Linux / Windows 均有 wheel，无需本地编译工具链。缺失时 `oauth_login` 导入即报错，错误消息提示 `pip install -r requirements.txt`。

本实现仅支持单进程 `threaded=True` 部署：续期线程与凭据文件均以进程内锁为前提，多 worker 会并发刷新并互相覆盖。

### D13：日志脱敏

- 应用日志与异常只输出状态与错误码，绝不输出 `code`、`secret`、`ticket_id`、`state`、`refresh_token`、`code_verifier`、`dpop_private_key`、`access_key_id`、`secret_access_key`、`security_token` 的值。
- 安装 werkzeug logger filter：对访问日志 query string 中的 `code` / `secret` / `ticket_id` / `state` 值替换为 `***`，防止 `/oauth/callback?code=...` 与 `/v1/login/ticket?...` 落入日志。
- 错误响应只返回固定文案，不回传上游原始响应体。
- OAuth 换码、刷新、ticket 轮询与身份补全的异常分支只记录错误类型名与 HTTP 状态码，不输出 Cookie 值、
  上游请求体或上游响应体；与既有 `admin_ui._fetch_credit_usage()` 的异常处理保持一致。
- 本变更新增流程不读取、不转发、不记录工作台 Cookie `devclouddevuibjJ_SESSION_ID`；既有积分查询仍按
  `config_store.get("CODEARTS_SNAP_SESSION")` 使用该 cookie，行为不变。

## 风险 / 权衡

| 风险 | 缓解 |
|---|---|
| 回调端口必须与 `redirect_uri` 一致 | 端口统一取自 `LISTEN_PORT`（即 `CODEARTS_PROXY_PORT`），授权链接与校验共用同一来源 |
| 代理绑定非回环地址时浏览器回调仍打到本机 | 保留 `import-callback` 手工导入；`/oauth/callback` 同时校验回环 Host 与回环 `remote_addr`，降低伪造面 |
| 307 开放重定向 | 校验目标为 https、host 等于 `PortalHost`、path 为 `/oauth/callback`、不含 userinfo |
| 多会话暴力尝试放大上游请求 | 候选上限 10 个；会话总数上限 20、单管理会话 5；超出返回 409 并记录不含敏感值的警告 |
| `refresh_token` 一次性轮换 | 刷新成功后若响应含新 `refresh_token` 立即替换并原子写回；写盘失败则保留内存新值并告警 |
| Flask `threaded=True` 并发访问凭据 | `oauth_login` 模块内 `threading.RLock` 保护内存态；对外只返回不可变快照 |
| 凭据文件泄露 | 0600 + 原子写 + 唯一 tmp 名；日志与异常只输出状态，不输出字段值；werkzeug 访问日志脱敏 |
| `cryptography` 安装失败 | 导入时报错并提示安装；管理页登录入口返回明确错误 |
| 多进程部署 | 明确仅支持单进程 `threaded=True`；写入前比较 `updated_at` 作为额外保护 |
| 启动刷新耗时或上游不可达 | 10 秒超时，失败不阻塞启动，由后台线程继续重试 |
| 服务重启导致内存会话丢失 | 回调提示重新发起登录；已登录凭据不受影响 |

## 迁移方案

1. `server.py` 删除 `CODEARTS_AK` / `CODEARTS_SK` 强校验；`benefit.py` 删除导入期强校验。
2. `config.json` 中遗留的 AK/SK 不再读取，`READONLY_SECRET_KEYS` 同步移除，配置页不再展示。
3. 既有测试中对 `server.AK` / `server.SK` 的引用改为通过 `oauth_login` 注入假凭据（模块级测试辅助函数或 `monkeypatch`）。
4. 已部署用户升级后需在管理页完成一次 OAuth 登录；登录前 `/v1/*` 返回 503。
5. 前端重建 `admin-ui/dist/`，旧哈希产物显式删除、新哈希显式加入提交。
6. 回滚：切回旧版本代码并在 `config.json` 中恢复 `CODEARTS_AK` / `CODEARTS_SK`；旧版本不读取 `oauth-auth.json`，该文件可保留也可删除。

## 待决问题

- 华为 `refresh_token` 的实际最长有效期未知。实现不依赖该值：无论有效期多长，终态失效都会转入「需重新登录」。可上线后通过观察日志确认。
