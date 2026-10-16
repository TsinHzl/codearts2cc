# 变更提案：add-admin-ui

## 背景
codearts2cc 目前是纯 API 代理：模型列表在 `models-cache.json`（300s TTL 自动刷新）、免费额度签到/余额查询在 `benefit.py`（CLI）、运行时配置散落在 `.env`（`CODEARTS_ANTHROPIC_MODEL_MAP`、`CODEARTS_CACHE_SIM` 等）。运维动作要么改文件重启、要么跑 CLI，缺少一个可视化管理面。kiro2cc-proxy 已有成熟 admin-ui，但其为「多租户转售」设计（多凭证池 / API Key 发放 / 负载均衡 / 用量计量 / IP 地理），核心数据对象在单账号的 codearts2cc 不存在，无法直接移植。本变更按用户确认的方向——「自建适配版」——针对 codearts2cc 的真实数据面重新构想一个最小可用的管理界面。

## 目标范围
**在范围内：**
- 模型列表页：展示双通道（agent / benefit）模型详情（id、name、channel、context_window、max_tokens、supports_images），含手动刷新
- 免费额度页：余额（总配额 / 已用 / 剩余）、签到状态，含「立即签到」写操作
- 配置页：`CODEARTS_ANTHROPIC_MODEL_MAP`（模型别名映射）与 `CODEARTS_CACHE_SIM`（缓存模拟开关）的查看与编辑
- 服务概览页：监听地址、上游端点、模型数、缓存开关态、启动时间等只读信息

**不在范围内：**
- kiro2cc-proxy 的多租户功能（凭证池、API Key 管理、负载均衡、用量计量/记账、IP 地理定位、日志流）——codearts2cc 无对应数据模型
- 真实 token 用量计费查询（上游 CodeArts 不返回逐次请求的 token 计量；`CODEARTS_CACHE_SIM` 仅模拟 prompt cache usage 字段，非真实计费）
- react / node 构建链的独立前端工程
- 代理主链路（`/v1/chat/completions`、`/v1/messages`）任何行为变更

## 技术方案
- 管理界面内嵌于 `server.py`：新增 `GET /admin`（单文件 HTML+CSS+JS）与一组 JSON 后端路由（`/api/admin/...`），复用现有 Flask 应用，零新依赖
- 前端为无构建单文件（原生 JS + fetch，跟随系统 prefers-color-scheme 深色模式），不引入 node/npm
- 鉴权：口令来自 env `ADMIN_PASSWORD`；未设置时 admin 界面禁用——`/admin` 返回 HTTP 403 禁用提示页、`/api/admin/*` 统一返回 403（语义「管理面未启用」，与「未登录 401」二分）；登录态用 HttpOnly + SameSite=Lax Cookie + 服务端 Session（Flask `session`，`ADMIN_SECRET_KEY` env 未设时随机生成）；登录接口加连续失败退避限流
- admin 蓝图豁免全局 `_cors`（`server.py:464` 对全部响应加 `Access-Control-Allow-Origin: *`）：admin 页面与 JSON 响应不携带 `Allow-Origin: *`，避免弱化 Origin 防线
- 配置写回与生效机制：编辑「模型映射（`CODEARTS_ANTHROPIC_MODEL_MAP`）/ 缓存模拟开关（`CODEARTS_CACHE_SIM`，位于 `cache_sim.py:32` 由 `cache_enabled()` 读取）」时，按行精准定位编辑 `.env`（不重写全文，避免触碰 `CODEARTS_AK`/`CODEARTS_SK` 行），写回后同步更新进程内 `os.environ`；由于两处键均由 `os.getenv` 即时读取（`anthropic_proxy.py:110`、`cache_sim.py:32`），同步 `os.environ` 后下一请求即生效，**无需改动代理主链路**；AK/SK 只读脱敏展示、禁止编辑
- 注意区分「缓存模拟开关」（`CODEARTS_CACHE_SIM`，本次配置编辑对象）与「模型列表缓存 300s TTL」（`server.py:347` 硬编码，不在本次编辑范围）
- 数据面复用：模型列表展示走现有 `SUPPORTED_MODELS`/`MODEL_DETAILS`；「手动刷新」调用 `_refresh_models()`（强制重新同步，绕过 `_refresh_models_if_needed` 的 300s TTL 门控，否则 300 秒内点击无实际效果）；额度/签到调用 `benefit.py` 的 `balance()`/`claim()`/`claim_status()`；概览页展示监听地址、上游端点、模型数（agent/benefit 分组）、缓存开关态与模型缓存更新时间
- benefit 引入方式：`benefit.py` 顶层有 `raise SystemExit`（缺 AK/SK）与 `load_dotenv(绝对路径)` 副作用；采用「函数级延迟 import + 测试 mock」方式，单测环境通过 monkeypatch 注入 `benefit` 模块的纯函数引用，避免顶层 import 触发副作用（详见 design.md）

## 预期影响
- 新增 `/admin` 与 `/api/admin/*` 路由；对既有 `/v1/*` 端点零改动
- 新增一个 `admin_ui.py` 模块（或拆分 `admin_ui.py` + `admin_ui_cli.py` 逻辑），由 `server.py` 末尾注册
- `.env` 新增可选项 `ADMIN_PASSWORD`、`ADMIN_SECRET_KEY`；不为空时 admin 界面启用，为空时禁用，不影响代理主链路
- 性能：admin 页面为低频运维操作，数据查询走现有缓存模型，无额外高频请求

## 风险
- **密钥泄露风险（高）**：管理界面读写含 AK/SK 的 `.env`。应对：AK/SK 仅脱敏只读展示、禁止编辑；`.env` 写入采用「按行精准编辑」不重写全文；admin 默认禁用（无 `ADMIN_PASSWORD` 不开放）；配置读取接口与服务端日志均不得回显完整 AK/SK
- **公网暴露放大（高）**：`LISTEN_HOST` 可被 `CODEARTS_PROXY_HOST` 覆盖为 `0.0.0.0`（`server.py:67`），admin 随 Flask 同端口注册，会与代理一起暴露到公网。应对：概览页检测监听地址为非回环时显示醒目警示；口令鉴权是唯一防线，登录接口加连续失败限流（退避延迟）
- **配置写回破坏格式（中）**：`.env` 行定位编辑若键不存在/格式异常可能破坏文件。应对：编辑前校验键是否已存在，只允许编辑两类已知键（映射、开关），写入前保留原行注释
- **CSRF / 会话安全（中）**：写操作用 Cookie Session，命中 auth/密钥敏感关键字，需 CR 专项审查。应对：登录/登出/写操作统一走服务端 Session 校验；`ADMIN_SECRET_KEY` env 未设时用随机值（重启会话失效，属可接受）；Cookie 设 `HttpOnly` + `SameSite=Lax`
- **SSRF（低）**：手动刷新固定调用现有 `_refresh_models()`（强制刷新），不接受客户端可控 URL/参数，无 SSRF 面
- **benefit.py 作为库引入（低）**：`benefit.py` 顶部有 `raise SystemExit`（缺 AK/SK 时）与 `load_dotenv` 副作用。应对：以函数级引入（import 后仅调用纯函数），不改动 benefit.py 的 CLI 行为；server.py 同缺 AK/SK 即退出，故该副作用实际不触发