# 变更提案：add-api-key-auth

## 背景

代理当前对 `/v1/*` 完全不鉴权：任何能连上监听端口（默认 `127.0.0.1`，可经 `CODEARTS_PROXY_HOST` 改为 `0.0.0.0`）的进程都能无门槛消耗本机 CodeArts 账号额度，使用日志也无法区分调用方。

参考同目录 `kiro2cc-proxy` 的子 Key 体系，本变更引入本地 API Key 鉴权与自助管理界面：用户在 admin 页面（口令登录）自行生成 key，把 Base URL + key 配到 Claude Code / OpenAI 客户端即可使用。管理面始终可用——首次使用时用户还没有 key，正是通过管理页面创建第一个 key，因此不存在"无 key 就进不去"的死锁。

**前提限定：** 上述"不存在死锁"仅在管理面可用（已设置 `ADMIN_PASSWORD`）时成立。未设置口令时管理面整体禁用（`/admin` 与 `/api/admin/*` 均 403），用户既无管理页面、也无任何带内手段关闭开关，若此时强制鉴权仍生效将造成无法自救的永久 401。因此鉴权门禁 SHALL 在管理面禁用时自动放行，并打印启动提示（见 `design.md` 决策 5）。

## 目标范围

**在范围内：**
- `/v1/*` 请求的 API Key 鉴权门禁（`x-api-key` 优先、`Authorization: Bearer` 次之；恒定时间比较）
- API Key 自助管理：生成（`sk-` + 32 位十六进制）、命名、启用/禁用、改名、删除、明文查看与复制
- 新增 admin「API Key」页签（桌面侧栏 + 移动端「更多」抽屉），页内完成全部管理操作
- 独立存储 `api-keys.json`（权限 0600），与 `config.json` 职责分离
- 强制鉴权开关 `CODEARTS_REQUIRE_API_KEY`（默认开启），可在配置页热切换

**不在范围内：**
- 消费限额（`spending_limit` / `limit_unit`）与有效期（`expires_at` / 懒激活 `duration_days`）——**已确认不落字段**：JSON 对象新增字段无兼容负担，在语义未定前不落无用字段。后续引入时按需增字段，无需迁移（见「需求裁决记录」）
- 独立 id 计数器文件——**已确认不建**：本变更不存按 key 的用量统计，删除后 id 复用无实际副作用；改用"当前列表最大 id + 1"，并在 `api_keys.py` docstring 记录"引入按 key 用量统计前必须先加 id 计数器"的前置约束（见「需求裁决记录」）
- 按 Key 维度的用量统计、子账号登录页与用量查询接口
- Key 与上游凭据绑定（`bound_credential_ids`）；本代理为单账号，无多凭据概念
- 启动即校验并退出、独立 CLI 管理工具（`benefit.py` 风格）——已明确由管理页面承担该职责
- 上游 AK/SK 与签名逻辑的任何改动

## 技术方案

- **存储**：新模块 `api_keys.py`，读写 `api-keys.json`（与 `config.json` 同目录，`tmp + os.replace` 原子写、`chmod 0600`、进程内 `threading.Lock`），风格对齐既有 `config_store.py` / `usage_log.py`。列表结构 `[{id, key, name, enabled, createdAt}]`。
- **鉴权点**：`server.py` 的 app 级 `before_request` 钩子统一拦截 `/v1/*`（`OPTIONS` 预检豁免），一处覆盖 OpenAI（`chat_completions`、`list_models`、`get_model`）与 Anthropic（`blueprint` 的 `/v1/messages`、`/v1/messages/count_tokens`）全部端点，避免逐路由重复。
- **错误协议**：401 响应按路径分流——`/v1/messages*` 返回 Anthropic 错误体，其余返回 OpenAI 错误体，客户端能正确显示。
- **管理端点**：`admin_ui.py` 新增 GET/POST/PATCH/DELETE `/api/admin/api-keys`，沿用既有口令会话鉴权与 `_json_error` 风格；`CODEARTS_REQUIRE_API_KEY` 加入 `EDITABLE_KEYS` 白名单。
- **前端**：新增 `admin-ui/src/tabs/ApiKeyTab.tsx`，`Layout.tsx` 的 `navDefs` 增加 `apikey` 页签（移动端自动落入「更多」抽屉），`npm run build` 重新产出 dist 入库。
- **约束**：`hmac.compare_digest` 做恒定时间比较并遍历全部 key（不提前 return）；key 明文只落 `api-keys.json`，不写日志、不进异常堆栈、不入 git。

## 预期影响

- **兼容性**：升级后 `/v1/*` 默认要求有效 key，未配置 key 的现有客户端会收到 401（错误信息指引到 admin 页面创建）。服务启动、admin 页面、口令登录、模型列表/额度/使用日志等管理功能**均不受影响**。
- **测试**：`test_openai.py` 冒烟脚本由"无鉴权"变为需携带 key，需同步适配（**脚本参数 `--key`**，缺省回退读 `api-keys.json` 首个启用 key；不引入 `CODEARTS_API_KEY` 环境变量，避免与"所有 `CODEARTS_*` 一律走 config.json"的既有约定冲突）。
- **文档**：`CLAUDE.md`、`openspec/project.md` 与 `README.md` 中"代理本身不鉴权 / API Key 填任意非空字符串"的描述需更新——README 的接入指引表与 Claude Code 示例会直接误导用户拿到 401。
- **忽略规则**：`.gitignore` 需同时忽略 `api-keys.json` 与其原子写的残留 `api-keys.json.tmp`（后者含 key 明文，与既有 `config.json.tmp` 同处理）。
- **性能**：每请求增加一次列表遍历 + 恒定时间比较，key 数量为个位数时开销可忽略。
- **依赖**：无新增第三方依赖（`secrets` / `hmac` / `uuid` 均为标准库）。

## 风险

| 风险 | 应对 |
|---|---|
| 默认强制鉴权导致升级用户客户端立刻 401，可能被误判为故障 | 401 消息明确写出"请在 /admin 的 API Key 页创建"，并在启用日志与文档中提示；配置页可一键关闭强制鉴权回退旧行为 |
| key 明文泄露（文件权限、日志、前端回显） | 文件 0600 且不入 git；日志与异常只含 key 的 id/name，不含明文；列表接口的明文仅经口令鉴权后的管理会话可读 |
| 无 key 状态下误判服务异常 | 该状态下 `/v1/*` 一律 401 并在消息中说明"尚未创建任何 API Key"，admin 页面正常可用 |
| 恒定时间比较被绕过（提前 return 造成时序侧信道） | 遍历全部条目累积比较结果，不做短路返回 |
| 非 ASCII 请求头导致 `hmac.compare_digest` 抛 `TypeError` → 500 | 比较前统一按 UTF-8 编码为 `bytes` 再比较（`compare_digest` 要求 `str` 为 ASCII-only）；补充"非 ASCII / 超长 key 返回 401 而非 500"的测试用例 |
| 管理面被禁用（未设 `ADMIN_PASSWORD`）时无自救路径 | 鉴权门禁在管理面禁用时自动放行，并在启动日志与 401 消息中说明如何启用管理面 |
| 非回环暴露（`CODEARTS_PROXY_HOST=0.0.0.0`）下鉴权面缺少 TLS 与限流 | 代理不内置 TLS 与鉴权失败退避，明文 HTTP 下依赖部署侧网络边界（防火墙 / 反向代理）保障；在 `README.md` 与 `design.md` 风险表中显式标注 |
| 403（禁用）与 401（未知）可区分 key 是否存在 | 本地回环场景风险可接受，保留 403 以便用户排查误禁用；该权衡在 `design.md` 决策 3 中显式记录 |
| 删除后 id 复用 | 本变更无按 key 的用量存储，id 复用暂无副作用；`design.md` 记录将来引入按 key 统计时必须同步引入 id 计数器 |

## 需求裁决记录

步骤 4.6 需求回溯核验发现两处"产物与澄清轮选项描述不一致"，经用户显式裁决，**以产物方案为准**（澄清轮选项中的附注属描述性说明，非需求）：

| 争议点 | 澄清轮选项描述 | 产物方案 | 用户裁决 |
|---|---|---|---|
| 限额/过期字段 | "限额/过期字段先落数据结构不启用" | 不落这四个字段 | **不落字段，保持最简**（推荐）——JSON 增字段无兼容负担，语义未定前不落无用字段 |
| 独立 id 计数器 | "单独文件 + 独立 id 计数器" | 不建计数器，用最大 id + 1 | **不建计数器，只记约束**（推荐）——当前无用例副作用，改为在 docstring 记录前置约束 |

两处均已在「不在范围内」显式标注，避免后续读者误认为遗漏。
