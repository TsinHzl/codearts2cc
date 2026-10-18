## 上下文

本代理是单账号本地服务（Flask，无数据库），已有两条对外数据面：OpenAI 兼容端点（`server.py` 内的 `/v1/chat/completions`、`/v1/models`）与 Anthropic 兼容端点（`anthropic_proxy.py` 蓝图的 `/v1/messages`、`/v1/messages/count_tokens`）。管理面（`admin_ui.py` 蓝图，`/admin` + `/api/admin/*`）已有成熟的口令鉴权、失败退避与配置原子写回机制。

参考实现 `../kiro2cc-proxy`（Rust/axum）的子 Key 体系同时涵盖鉴权、限额、有效期与按 key 用量统计。本代理无多凭据、无按 key 计费需求，故按最小可用的鉴权 + 自助管理切片移植。存储结构本次只落 `{id, key, name, enabled, createdAt}` 五个字段（不预留空字段）；JSON 对象后续新增字段对旧数据无兼容负担，需要时直接加即可。

## 目标 / 非目标

**目标：**
- 一处拦截覆盖全部 `/v1/*` 端点，不遗漏新端点
- 管理面完全不受鉴权影响，用户永远能打开页面创建/管理 key
- key 明文不出现在日志、异常、版本控制中
- 无第三方新依赖

**非目标：**
- 消费限额、有效期/懒激活、按 key 用量、子账号登录
- 多凭据绑定、分布式部署下的 key 同步（本代理是单进程本地服务）

## 决策

### 决策 1：存储落在独立 `api-keys.json`，不并入 `config.json`

新增模块 `api_keys.py` 作为唯一读写入口。

- **理由**：key 列表是频繁增删的运行时数据，`config.json` 是配置项集合（且其读接口按 `EDITABLE_KEYS` 白名单暴露给前端）。混在一起会让 key 明文进入配置页的数据通路，违背"密钥不出配置面"的既有约束。独立文件也让"key 文件不入 git"这一条更容易在 `.gitignore` 里表达清楚。
- **路径注入**：镜像 `config_store.init(path)` 模式——模块级 `_path` 由 `server.py` 启动时 `api_keys.init(API_KEYS_PATH)` 注入（路径与 `config.json` 同目录）。未 init 时所有读写为无害空操作（`list_keys()` 返回 `[]`、`authenticate` 返回"未匹配"），保证只 import 不启动的场景（单测、工具脚本）不炸。
- **原子写与权限时序（必须按此顺序）**：`os.open(tmp, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` 创建 tmp → 写入 → `os.replace(tmp, path)`。**先建 0600 的 tmp 再 replace**，不能在 replace 之后才 `chmod`——那会在两者之间留下一个明文文件为 umask 默认权限（通常 0644）的可读窗口。与 `server.py:_session_salt` 的盐文件写法一致。
- **失败回滚**：写盘抛 `OSError` 时回滚内存态到改动前的快照并向上抛出（或返回错误），保证内存与磁盘一致，避免"管理页面显示已创建、重启后消失"。
- **读写入口收口**：`list_keys()` 返回深拷贝，避免调用方原地修改内存态。

### 决策 2：鉴权挂在 app 级 `before_request`，不做逐路由装饰器

在 `server.py` 注册一个 `before_request` 钩子，对 `request.path` 以 `/v1/` 开头的请求统一校验。

- **理由**：数据面端点分散在两个模块（`server.py` 路由 + `anthropic_proxy.py` 蓝图），逐路由加装饰器容易在后续新增端点时漏加（漏加即静默无鉴权，是安全缺陷）。钩子只认路径前缀，新增任何 `/v1/*` 端点自动被覆盖。
- **豁免清单**：`OPTIONS` 方法（CORS 预检不带自定义头）、非 `/v1/` 路径（管理面与静态资源）。
- **返回分流**：路径以 `/v1/messages` 开头时返回 Anthropic 错误结构（`{"type":"error","error":{"type":...,"message":...}}`），否则返回 OpenAI 结构（`{"error":{"message":...}}`），因为两类客户端的错误解析器不兼容，统一格式会让其中一方显示不出可读信息。
- **401 消息区分两种状态**：key 列表为空时提示"尚未创建任何 API Key，请在 /admin 的 API Key 页生成"；列表非空但候选无匹配时提示"无效的 API Key"。前者直接指向解法，避免首次用户误判服务故障。
- **必须同步更新 CORS 白名单（易漏）**：`server.py:_cors` 的 `Access-Control-Allow-Headers` 现为 `"Content-Type, Authorization"`。由于本方案把 `x-api-key` 定为优先头，浏览器客户端（LobeChat 等跨域工具）携带该头会在**预检阶段**被拒——`OPTIONS` 虽已豁免鉴权，但预检响应头白名单不含 `x-api-key` 时浏览器仍不放行实际请求。因此必须把 `x-api-key` 加入该白名单，并补一条预检用例（`Origin: http://localhost:3000` + `Access-Control-Request-Headers: x-api-key`）验证。

### 决策 3：恒定时间比较 + 全量遍历 + 编码归一

用 `hmac.compare_digest` 逐条比较，遍历完整个列表累积结果，不提前 `break`/`return`。比较前双方统一 `encode("utf-8")` 转为 `bytes`。

- **理由**：字典查找或提前返回会让比较耗时随"前缀匹配长度"变化，构成时序侧信道；虽然本地服务的威胁模型较弱，但成本仅是个位数次比较，没有理由不做。
- **编码归一是必需的，不是防御性冗余**：`hmac.compare_digest` 对 `str` 入参要求 ASCII-only，含非 ASCII 字符时抛 `TypeError`；而 Werkzeug 按 latin-1 解码请求头，`x-api-key` 可携带 0x80–0xFF 字节。任意客户端发一个高位字节即可把 401 变成未捕获异常的 500。统一转 `bytes` 后 `compare_digest` 对任意字节串都不抛异常，长度不同时直接返回 False。
- **超长 key**：长度差异由 `compare_digest` 内部处理为 False，不额外做长度上限校验（请求头本身受 Werkzeug 大小限制约束）。
- **已知权衡：403 与 401 可区分 key 是否存在**。禁用 key 返回 403、未知 key 返回 401，理论上允许探测"某 key 是否存在"。这与本决策的时序侧信道抑制口径不完全一致。取舍理由：本地回环单用户场景下，403 对"我以为启用了却被禁用"的排查价值高于该信息泄露的实际危害；若将来暴露到非受信网络，应统一改为 401 并仅在管理页面呈现禁用状态。

### 决策 4：不引入 id 计数器文件（与蓝本的偏离）

蓝本用 `api_key_id_counter.json` 保证删除后 id 不复用，原因是其 `api_key_usage.json` 按 `api_key_id` 存用量，复用 id 会让新 key 继承旧用量并误判超限。

本代理本变更不存储按 key 的用量（使用日志只记 IP / 账号 / 模型），id 复用无副作用，因此不引入计数器。id 生成用"当前列表最大 id + 1"。

- **约束记录**：将来引入按 key 用量统计时，必须先加 id 计数器，否则会出现用量串号。该约束写在 `api_keys.py` 模块 docstring 中。
- **该取舍经用户显式裁决**（步骤 4.6，选项为"不建计数器，只记约束"）：澄清轮选项描述中虽提及"独立 id 计数器"，但那是蓝本实现说明的一部分，非本次需求；用户确认以最简方案落地。若将来统计需求出现，须按上述约束先补计数器再接入统计。

### 决策 5：强制鉴权开关为配置项，默认开启；管理面禁用时自动放行

`CODEARTS_REQUIRE_API_KEY` 加入 `admin_ui.EDITABLE_KEYS` 与 `_EDITABLE_DEFAULTS`（默认 `"1"`），取值限定 `"0"` / `"1"`（与其他白名单键的单行校验同层，非法值拒绝保存）。

- **理由**：默认开启满足安全预期；但升级用户的客户端不会立刻断连（他们先去管理页建 key），且一旦出问题可在配置页一键关掉回到旧行为，不必改代码或重启。
- **读取时机**：每次请求实时 `config_store.get(...)` 读取，保证热生效（与 `CODEARTS_SNAP_SESSION` 同策略）。
- **兜底规则（必需）**：管理面禁用时（未设置 `ADMIN_PASSWORD`）门禁整体跳过校验。理由链：管理面禁用意味着用户既打不开 `/admin` 也没有 `/api/admin/*` 可用，而本变更明确不做启动校验、不做 CLI（见非目标），此时强制鉴权会让用户陷入无法自救的永久 401——既无带内手段创建 key，也无带内手段关闭开关。跳过校验的同时在**启动日志**打印提示（说明鉴权未生效及启用管理面的方式）。注意：该分支下门禁不产生 401，因此该提示只出现在启动日志，不写入任何 401 响应体。
- 该兜底不削弱安全目标：管理面禁用本身就意味着运维者放弃了管理入口，此时代理退化为纯本地转发（与引入鉴权前的行为一致），符合"不制造比变更前更差的可用性"原则。

### 决策 6：key 生成用 `secrets.token_hex(16)`

格式 `sk-` + 32 位十六进制，与蓝本一致。`secrets` 是 CSPRNG，适合生成凭据；`uuid4` 也够用但 `secrets` 语义更明确。

### 决策 7：管理页面明文可见

列表接口返回 key 明文，页面提供复制按钮。

- **理由**：本地单用户工具，"创建后再也看不到"会迫使用户反复重建 key。保护边界是管理口令会话，与 AK/SK 的"只读脱敏"不同——AK/SK 脱敏是因为用户无需复制它（它配在自己的云账号侧），而 API Key 的用途就是复制到客户端。
- **补偿**：key 明文不写日志、不进异常堆栈；`api-keys.json` 0600 且加入 `.gitignore`。

## 风险 / 权衡

| 权衡点 | 取舍 |
|---|---|
| 默认强制鉴权带来升级摩擦 vs 默认关闭等于没做 | 选前者；用"401 消息带指引 + 配置页一键关闭 + 管理面禁用时自动放行"降低摩擦 |
| 明文在列表接口返回 vs 只返回掩码 | 选前者；本地工具可用性优先，边界靠口令会话与文件权限 |
| app 级钩子拦截 `/v1/*` vs 逐路由显式声明 | 选前者；防"新增端点漏加鉴权"的静默缺陷，代价是豁免规则需集中在钩子内维护 |
| 不做限额/有效期 vs 一次做全 | 选前者；本次目标是"能安全地限定调用方"，限额属于计费维度，需求未确认前不引入 |
| 403（禁用）可区分于 401（未知）vs 统一 401 | 本地回环场景保留 403 的排查价值；非受信网络部署时应改统一 401（见决策 3） |
| 明文 HTTP、无鉴权失败退避 vs 内置 TLS/限流 | 不内置。代理定位是本地回环服务；`CODEARTS_PROXY_HOST=0.0.0.0` 属用户主动暴露，安全边界交给部署侧（防火墙 / 反向代理），在 README 显式标注 |
| 与在途变更 `align-admin-ui-codex2cc` 的 spec delta 基底冲突 | 两者都 MODIFIED 同一 Requirement「管理页面技术实现」，且 align 的 delta 措辞（"五个页签"）与 live spec（"概览/模型/额度/配置四个"）不一致。**归档顺序约定：本变更先归档**——本 delta 的 `← (was ...)` 标注以 live spec 为准；若 align 先归档，则本 delta 的 was 标注需按 align 落地后的文本改写。详见下方"跨变更依赖" |

## 迁移方案

1. 升级后服务正常启动，admin 页面与口令登录不受影响。
2. 用户在 admin「API Key」页签生成 key（默认即启用）。
3. 客户端配置：Base URL 保持 `http://127.0.0.1:51818/v1`，API Key 填生成的 `sk-...`。
   - Claude Code（Anthropic 端点）：`ANTHROPIC_BASE_URL=http://127.0.0.1:51818`、`ANTHROPIC_API_KEY=sk-...`
   - OpenAI 兼容客户端：Base URL `http://127.0.0.1:51818/v1`、API Key `sk-...`
4. 若需暂时回退旧行为：配置页将 `CODEARTS_REQUIRE_API_KEY` 置 `0`，无需重启。

## 跨变更依赖

`openspec/changes/align-admin-ui-codex2cc/` 处于 `IN_PROGRESS`，其 delta 同样 MODIFIED `openspec/specs/admin-ui/spec.md` 的「管理页面技术实现」Requirement，措辞为"五个页签（概览/模型/额度/使用日志/配置）"。当前 live spec 仍是"概览/模型/额度/配置四个"。

- **约定归档顺序：本变更（add-api-key-auth）先归档**，之后再归档 align。本 delta 的 `← (was 概览/模型/额度/配置四个页签)` 标注与 live spec 一致，可直接合并。
- 若实际顺序相反（align 先归档），归档本变更时需把 was 标注与 delta 正文改写为 align 落地后的文本（"五个页签"→"六个页签"）。
- 两者不冲突的实质原因：本变更只是在 align 的页签列表基础上再加一个 `apikey` 页签，Requirement 的其他内容（Vite 构建、Tailwind token 层、静态资源缓存策略）完全相同。

## 待决问题（Open Questions）

无。范围、默认值、存储位置、首个 key 的创建路径、跨变更归档顺序均已确认，不影响 spec 与任务拆分。
