# 任务清单：add-oauth-login

## 状态：IN_PROGRESS

## 任务

### 1. 依赖与忽略规则
- [x] 更新 `requirements.txt` 增加 `cryptography>=42`（`requirements-dev.txt` 已通过 `-r requirements.txt` 继承）；验证：`python3 -c "import cryptography; print(cryptography.__version__)"` 输出 ≥ 42。
- [x] 更新 `.gitignore` 增加 `/oauth-auth.json`、`/oauth-auth.json.tmp` 与 `/oauth-auth.json.tmp.*`；验证：`git check-ignore -v oauth-auth.json` 有输出。

### 2. 协议层（TDD）

#### 2.1 RED：协议层测试
- [x] 新增 `test_oauth_login.py`，覆盖：
  - PKCE：64 字节 verifier、base64url 无填充、challenge = `base64url(sha256(verifier))`。
  - DPoP JWT：header 含公钥 JWK、payload 的 `htm == "POST"`、`htu == "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"`（完整 URL、无 query、无尾斜杠）、`jti` 为 32 位 hex、ES256 低 S、JWK 还原与校验。
  - 授权链接：参数完整集（`theme`、`locale`、`uri_scheme`、`client_id`、`port`、`code_challenge`、`code_challenge_method`、`state`、`ticket_id`、`plugin-name`、`plugin-version`），`port` 取自 `LISTEN_PORT`。
  - 换码表单含 `redirect_uri` 与 `code_verifier`；刷新表单含 `code_verifier`、`client_id`、`refresh_token`。
  - ticket 响应 legacy 字段归一化；终态失效判定。
  - 固定签名向量：空 body 的 `X-Sdk-Content-Sha256` 为 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`，非空 body 为 `sha256(body).hexdigest()`。
- [x] 验证：`python3 -m pytest test_oauth_login.py -q` 先运行得到失败。

#### 2.2 GREEN：协议层实现
- [x] 新增 `oauth_login.py` 协议层实现：常量、PKCE、DPoP ES256 JWT 签名（`ec.SECP256R1` + `ec.ECDSA(hashes.SHA256())`，DER 拆 r/s、`s > N/2` 时取 `N - s`、各补零至 32 字节大端拼接）、授权链接构造、换码与刷新请求、ticket 轮询（不签名，仅带 `plugin-name` / `plugin-version` 头）、身份补全（优先 `GET {sts}/v5/caller-identity`，其次 `GET {snap_manager}/v1/current/user`，均用同一 STS 凭据签名，均失败解析 refresh_token JWT 的 `sub`）；导入 `cryptography` 失败时抛出带安装提示的错误。
- [x] 验证：`python3 -m pytest test_oauth_login.py -q` 通过。

### 3. 凭据存储

#### 3.1 存储接口与原子写
- [x] 在 `oauth_login.py` 实现 `init(path)` 注入凭据路径、`oauth-auth.json` 原子写（tmp 名 `oauth-auth.json.tmp.<pid>.<随机 hex>` + `os.replace`）与 0600、模块内锁、`current_credentials()` 快照、`credentials_status()`、`CredentialsUnavailable`；`expiration` 以 RFC3339 UTC 字符串落盘、读取时解析为带时区 `datetime`。
- [x] 验证：存储用例断言权限 0600、重启后读取、损坏文件降级、未调用 `init()` 抛 `RuntimeError`、日志与异常不含字段值；全部使用 `tmp_path` 隔离。

#### 3.2 刷新凭据字段完整性
- [x] 实现刷新请求所需的 `code_verifier` 与 `client_id` 持久化，并在刷新表单中验证；`refresh_token` 缺失时跳过刷新。
- [x] 验证：断言刷新表单含 `client_id`、`code_verifier`、`refresh_token`，且复用签发时的 DPoP 私钥。

### 4. 会话仓库与续期

#### 4.1 OAuthSessionStore
- [x] 在 `oauth_login.py` 实现 `OAuthSessionStore`：15 分钟 TTL、`state`（`secrets.token_hex(16)`）与管理会话绑定、secret/ticket_id/创建时间索引、候选顺序（secret → ticket_id → 创建时间倒序，上限 10）、会话总数上限 20、单管理会话活跃上限 5、`ticket_fallback` 门控、每次 poll 最多 1 次 ticket 请求。
- [x] 验证：TTL 过期、候选匹配、上限 409、跨管理会话访问被拒、ticket 门控用例通过。

#### 4.2 refresh_if_needed()
- [ ] 实现 `refresh_if_needed()`：`remaining <= 30min`（含已过期）触发刷新；瞬时错误 5 分钟退避；刷新前重读文件比较 `updated_at`；终态失效置 `needs_relogin` 并停止重试；支持注入时钟。
- [ ] 验证：到期前刷新、已过期仍刷新、5 分钟退避、终态失效不再发请求、跨进程 `updated_at` 较旧者放弃写入。

### 5. 回调路由
- [ ] 在 `server.py` 注册 `/oauth/callback`：回环 Host 与回环 `remote_addr` 双重校验、首次 secret 阶段 307（校验目标为 https、host 等于 PortalHost、path 为 `/oauth/callback`、不含 userinfo）、第二次 code 阶段按候选换码、响应头 `Cache-Control: no-store` 与 `Referrer-Policy: no-referrer`、HTML 参数转义。
- [ ] 验证：新增 `test_oauth_callback.py` 覆盖两阶段、非回环 Host 403、非回环 `remote_addr` 403、非法 redirect 拒绝、响应头存在。

### 6. 签名改造与未登录降级

#### 6.1 签名改造
- [ ] 改造 `server.py`：移除 AK/SK 启动强校验，`_signed_request` / `_hmac_headers` 改用 STS 快照，`security_token` 非空时附加 `X-Security-Token`，并始终附加 `X-Sdk-Content-Sha256`；聊天 POST、模型发现 GET 均附加并参与签名。
- [ ] 验证：固定向量单测覆盖「聊天 POST + benefit 三头（含 `X-Security-Token`）」「模型发现 GET」两类签名组合，断言 `X-Sdk-Content-Sha256` 存在且在 `SignedHeaders` 中；另断言 ticket GET 仅含 `plugin-name` / `plugin-version`，且无 `Authorization` / `X-Security-Token` / `X-Sdk-Content-Sha256`；空 body 断言常量哈希。

#### 6.2 日志脱敏
- [ ] 在 `server.py` 安装 werkzeug logger filter：访问日志 query string 中的 `code` / `secret` / `ticket_id` / `state` 值替换为 `***`；OAuth 换码、刷新、ticket 轮询与身份补全的异常分支只记录错误类型名与 HTTP 状态码，不输出 Cookie 值、上游请求体或上游响应体。
- [ ] 验证：用例构造含参访问日志并断言脱敏后不含原值；另以 `caplog` / `capsys` 断言网络异常、上游非 200 与 JSON 解析失败三条路径的日志与异常栈不含 Cookie 值、请求体与响应体内容。

#### 6.3 未登录降级
- [ ] `/v1/chat/completions` 返回 OpenAI 503 信封（`{"error": {"message": "尚未登录 CodeArts，请先在管理页完成登录", "type": "service_unavailable", "code": "oauth_not_logged_in"}}`）；`/v1/messages` 返回 Anthropic 503 信封，并在 `anthropic_proxy.py` 中把 `signed_headers_fn` 调用（位于 `try` 块外）一并纳入捕获；`/api/admin/benefit`、`/api/admin/benefit/claim`、`/api/admin/credit` 返回可读 503；`/v1/models` 与 `/v1/models/<id>` 不返回 503，直接返回缓存或兜底列表；模型刷新回退缓存。
- [ ] 验证：无凭据启动用例断言服务可启动且上述端点不抛 500。

### 7. 管理端点

#### 7.1 端点实现
- [ ] 新增 `admin_oauth.py`：`start`（生成 state 并绑定当前管理会话）/ `poll`（短轮询立即返回）/ `import-callback` / `logout` / `status`，嵌套蓝图继承 `admin_ui` 口令会话。
- [ ] 验证：新增 `test_admin_oauth.py` 断言未登录 401、start 响应不含敏感字段、跨管理会话 409、logout 后状态为未登录。

#### 7.2 错误契约与上限
- [ ] 实现错误契约（401 / 403 / 400 / 409 / 502 / 503）与会话总数 20、单管理会话 5 的上限，超出返回 409。
- [ ] 验证：逐条断言状态码与错误体，import-callback 非回环、端口不符、缺 code 返回 400。

### 8. 续期线程与启动
- [ ] `server.py` 启动时立即执行一次 `refresh_if_needed()`（10 秒超时，失败不阻塞启动），并在 `__main__` 中启动 daemon 续期线程（30 分钟轮询、提前 30 分钟、已过期同样尝试），用 `WERKZEUG_RUN_MAIN` 防止 Flask reloader 重复起线程；未设置 `ADMIN_PASSWORD` 时打印可操作警告。
- [ ] 验证：导入 `server` 不产生后台线程；`__main__` 路径下线程存在；reloader 场景只起一个线程；未设口令时启动日志含警告且服务正常监听。

### 9. benefit.py
- [ ] 改造 `benefit.py`：移除导入期 AK/SK 强校验，`signed_request` 改用 OAuth 凭据并附加 `X-Security-Token` 与 `X-Sdk-Content-Sha256`，CLI 无凭据时输出可读提示并非零退出；验证：`python3 benefit.py status` 在无凭据时输出可读提示且不抛堆栈。

### 10. 管理面与前端

#### 10.1 admin_ui.py
- [ ] 改造 `admin_ui.py`：`READONLY_SECRET_KEYS` 移除 `CODEARTS_AK` / `CODEARTS_SK`，状态接口返回 OAuth 登录状态；调整 `_admin_gate`：未配置 `ADMIN_PASSWORD` 时放行 `/admin` 页面壳与 `/admin/` 前缀静态资源，仅 `/api/admin*` 返回 403；`_fetch_credit_usage()` 保持不变（仍读取 `CODEARTS_SNAP_SESSION` 作为 `devclouddevuibjJ_SESSION_ID` cookie 值），本变更新增流程不读取该 cookie；验证：`test_admin_ui.py` 配置页用例通过，且未配置口令时 `GET /admin` 返回 200、`GET /api/admin/oauth/status` 返回 403，积分查询用例仍断言该 cookie 名精确拼写。

#### 10.2 前端
- [ ] 前端新增「登录」页签（状态卡、登录按钮、授权链接展示、1.5 秒间隔短轮询、回调 URL 导入、退出登录）并重建 `dist/`；验证：`cd admin-ui && npm run build` 成功，产物哈希更新。

### 11. 既有测试
- [ ] 更新既有测试：`test_admin_ui.py`、`test_config_store.py`、`test_api_key_gate.py` 中断言 `CODEARTS_AK` / `CODEARTS_SK` 的用例改为 OAuth 状态或假凭据；`test_openai.py` 冒烟脚本标注需先完成 OAuth 登录；验证：`python3 -m pytest test_admin_api_keys.py test_admin_ui.py test_anthropic_proxy.py test_api_key_gate.py test_api_keys.py test_cache_sim.py test_config_store.py test_oauth_login.py test_oauth_callback.py test_admin_oauth.py test_pricing.py test_usage_log.py -q` 全绿。

### 12. 文档
- [ ] 更新 `README.md`、`start.sh`、`config.example.json`、`CLAUDE.md` 与 `openspec/project.md`：删除 AK/SK 获取与必填说明，改为「登录」页签 OAuth 登录，并说明首次使用路径（设 `ADMIN_PASSWORD` → 重启 → 打开 `/admin` → 「登录」页签）；验证：上述文件中不再出现「启动缺失 AK/SK 即退出」的描述。

## 验收标准

- [ ] 未配置任何 OAuth 凭据时 `python3 server.py` 正常启动，`/admin` 可打开并展示「未登录」状态与登录入口。
- [ ] 未设置 `ADMIN_PASSWORD` 时服务仍正常启动、`/admin` 页面壳与静态资源可访问、`/api/admin/*` 返回 403，启动日志打印可操作警告；不采用「启动即退出」。
- [ ] 在管理页点击登录后可获得授权链接，浏览器完成授权后管理页显示已登录、账号名与到期时间。
- [ ] 登录后 `/v1/chat/completions` 与 `/v1/messages` 均能正常返回，mock 上游断言请求头含 `X-Security-Token` 与 `X-Sdk-Content-Sha256`，且都出现在 `SignedHeaders` 中。
- [ ] 未登录时 `/v1/models` 与 `/v1/models/<id>` 返回缓存或兜底列表（不返回 503），聊天补全与 Messages 返回 503。
- [ ] 模型发现、签到与余额查询成功路径均可走通（mock 上游），且使用同一 STS 凭据。
- [ ] ticket 兜底路径可完成登录（mock ticket 响应），并正确处理缺失 refresh_token 的情况。
- [ ] `import-callback` 手工导入回调 URL 可完成登录。
- [ ] ticket 轮询请求仅携带 `plugin-name` / `plugin-version` 头，不参与签名（mock 上游断言无 `Authorization` / `X-Security-Token` / `X-Sdk-Content-Sha256`）。
- [ ] 使用可注入时钟把 expiration 调到 30 分钟内（含已过期），`refresh_if_needed()` 触发一次刷新并写回新的 `expiration`。
- [ ] 模拟 `invalid_grant` 后 `credentials_status()` 返回需重新登录，且连续调用不再产生刷新请求。
- [ ] 服务重启后复用 `oauth-auth.json` 中的凭据，无需重新登录。
- [ ] `oauth-auth.json` 权限为 0600；`caplog` 与 `capsys` 断言刷新、换码、异常路径与 werkzeug 访问日志均无 `code` / `secret` / `ticket_id` / `refresh_token` / `secret_access_key` / `security_token` 明文，且异常栈不含 Cookie 值、上游请求体或上游响应体内容。
- [ ] 新增 OAuth 流程不读取、不转发、不记录 `devclouddevuibjJ_SESSION_ID`；既有积分查询仍按 `CODEARTS_SNAP_SESSION` 使用该 cookie（mock 上游断言 cookie 名精确拼写）。
- [ ] 管理面任一 `/api/admin/oauth/*` 响应不包含 `refresh_token`、`code_verifier`、DPoP 私钥或 STS 明文。
- [ ] 既有 API Key 门禁、管理端口令鉴权、模型/额度/使用日志/配置页签行为不变。
- [ ] 上述 pytest 命令全绿，AI 相关新增模块测试覆盖率 ≥ 80%。
