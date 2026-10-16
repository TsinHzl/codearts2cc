# 任务清单：add-admin-ui

## 状态：ARCHIVED

## 任务
- [x] T1 `admin_ui.py` 鉴权中间件与骨架：Flask Blueprint（`/admin` + `/api/admin/*`），`ADMIN_PASSWORD` 空时 `/admin` 返回 HTTP 403 禁用提示页、`/api/admin/*` 统一 403；已设置时未登录返回 401、登录成功后建会话（Cookie 设 `HttpOnly`+`SameSite=Lax`）、登录连续失败退避限流；admin 蓝图豁免全局 `_cors` 的 `Allow-Origin: *`；验证：`pytest test_admin_ui.py` 覆盖「未设口令禁用 403」「未登录 401」「口令登录成功」「错误口令拒绝」「退避限流」五例
- [x] T2 模型聚合与刷新接口：`GET /api/admin/models` 按 `SUPPORTED_MODELS`/`MODEL_DETAILS` 分组输出（agent/benefit），含 id/name/channel/context_window/max_tokens/supports_images；`POST /api/admin/models/refresh` 调用 `_refresh_models()`（强制刷新，绕过 300s TTL），不接受客户端 URL/参数；验证：单元测试模型分组正确 + 刷新触发强制 re-sync
- [x] T3 额度与签到接口：封装 `benefit.balance()`/`claim_status()`/`claim()` 为 `GET /api/admin/benefit`（余额+状态）与 `POST /api/admin/benefit/claim`（签到）；benefit 采用函数级延迟 import + monkeypatch 注入，失败转 JSON 错误（签到失败不伪装成功）；验证：mock benefit 纯函数，余额/签到/失败三例
- [x] T4 `.env` 精准读写与配置接口：按行定位读写 `.env`（键存在才写、保留注释、CRLF/行内注释妥善处理、不重写全文、AK/SK 只读脱敏且日志不回显），`GET/PUT /api/admin/config` 覆盖 `CODEARTS_ANTHROPIC_MODEL_MAP` 与 `CODEARTS_CACHE_SIM`，写后同步 `os.environ` 即时生效；验证：单测临时 .env 文件行编辑、脱敏、键不存在拒绝、AK/SK 行不被改写
- [x] T5 前端单文件 `/admin`：原生 HTML+CSS+JS（fetch + prefers-color-scheme 深色），四页签（概览/模型/额度/配置），登录表单，403/401 状态分区提示，写操作防重复提交；验证：curl `/admin` 返回 200 与 HTML 骨架含四页签标记
- [x] T6 `server.py` 装配与集成回环：末尾注册 `admin_ui.blueprint`，读取 `ADMIN_PASSWORD`/`ADMIN_SECRET_KEY`（未设 secret 用随机值），概览注入监听地址/上游端点/模型数（双通道分组）/缓存开关态/模型缓存更新时间；验证：启动服务走通「登录 → 四页签 → 签到 → 改配置写回 .env（先备份/恢复，验证 AK/SK 行字节不变）→ 重启前生效」回环

## 验收标准
- [x] `ADMIN_PASSWORD` 未设置时：`GET /admin` 返回 HTTP 403 禁用提示页，`GET /api/admin/models` 返回 403，响应体不含任何模型/额度/配置数据
- [x] `ADMIN_PASSWORD` 设置后：未登录 `GET /api/admin/*` 返回 401；正确口令登录后访问四页签接口均 200；错误口令不建立会话；密码连续错误触发退避限流
- [x] `GET /api/admin/models` 返回的对象按 channel 分 `agent`/`benefit` 两组，每组含 id/name/context_window/max_tokens/supports_images 字段
- [x] `GET /api/admin/benefit` 返回余额与签到状态；`POST /api/admin/benefit/claim` 签到成功返回最新余额与状态，失败返回非 2xx 及可读错误信息
- [x] 服务概览页展示监听地址、上游端点、模型数（agent/benefit 分组）、缓存开关态、模型缓存更新时间五类只读信息
- [x] `PUT /api/admin/config` 修改模型映射或缓存开关后：`.env` 中对应行精确更新且 `CODEARTS_AK`/`CODEARTS_SK` 行字节不变，进程内 `os.environ` 同步，下一请求即按新映射/开关生效；admin 响应不含 `Access-Control-Allow-Origin: *`
- [x] 既有 `/v1/models`、`/v1/chat/completions`、`/v1/messages` 行为零变化：`python test_anthropic_proxy.py` + `python test_cache_sim.py` 全绿，`python test_openai.py`（启动服务后）通过