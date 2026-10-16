## 上下文
codearts2cc 是纯 Python Flask 代理，无数据库、无 node 构建链。管理界面需零新依赖、内嵌进现有 `server.py` 单进程，同时不触碰含 AK/SK 的 `.env` 敏感行。

## 目标 / 非目标
- 目标：提供最小可用管理面（概览/模型/额度/配置），口令鉴权，配置写回 `.env`。
- 非目标：多租户转售功能、真实 token 计量、独立 React 前端工程。

## 决策
1. **内嵌单文件前端**：选 Flask 内嵌单文件（原生 JS + fetch + prefers-color-scheme），不引入 node/npm。理由：仓库无前端构建链，单文件零依赖、可随服务分发。
2. **Session 鉴权用 Cookie**：Flask `session`（HttpOnly + SameSite=Lax Cookie）+ `ADMIN_PASSWORD` 口令。`ADMIN_SECRET_KEY` env 未设时随机生成（重启会话失效，可接受）；登录接口加连续失败退避限流。理由：满足「口令鉴权」且最简，兼顾暴力破解防护。
3. **`.env` 按行精准编辑**：仅允许编辑两类白名单键（`CODEARTS_ANTHROPIC_MODEL_MAP`、`CODEARTS_CACHE_SIM`），键不存在时拒绝写；AK/SK 行只读脱敏、禁止编辑且日志不回显。写后同步 `os.environ`（两键均即时 `os.getenv` 读取，无需改主链路）。理由：避免重写全文造成密钥丢失/格式破坏。
4. **benefit 复用**：以模块级导入 `benefit.py` 的 `balance()`/`claim()`/`claim_status()` 纯函数，不改动其 CLI 行为。理由：避免签到逻辑重复实现。
5. **禁用策略**：`ADMIN_PASSWORD` 为空时 `/admin` 返回 403 禁用提示页、`/api/admin/*` 拒绝访问——默认不开放管理面，降低密钥暴露风险。

## 风险 / 权衡
- 写回 `.env` 是安全敏感操作（auth/密钥关键字命中，CR 需专项审查）；权衡：限制为白名单键 + 行级编辑 + 默认禁用，换取「运行时配置 GUI 化」的运维便利。
- Cookie Session 无 CSRF token，本地服务（仅绑 127.0.0.1）跨站风险低；权衡：接受，CR 中评估是否需加 SameSite 强化。

## 迁移方案
无需迁移：新增文件与路由，既有 `/v1/*` 端点零改动；`.env` 仅新增可选键，缺省时功能禁用。

## 待决问题
无。

## 附：留待实施阶段落实的细节（不影响 spec / 方案 / 任务拆分）
- `.env` 白名单键在文件中不存在时的行为：配置页前端提示「需手动在 .env 添加该键」，后端「键存在才写」拒绝写不存在的键（不自动追加，避免格式风险）
- `.env` 行编辑对 CRLF / 行内注释 / 值引号的保留：读取时按行原文保留，编辑仅替换 `=` 后的值部分，其余字节不动
- 会话/限流为进程内内存态，`threaded=True` 下用线程锁保护；`ADMIN_SECRET_KEY` 未设时随机生成 → 重启后需重新登录
- 集成验收（T6）写回 `.env` 前自动备份、完成后恢复，并断言 AK/SK 行字节不变