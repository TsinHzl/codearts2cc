# codearts2cc

把华为云 CodeArts 的模型接口包装成 **OpenAI + Anthropic 双协议本地代理**，让 Claude Code、OpenCode、Cline 等智能体直接接入 CodeArts 套餐模型（盘古 2.0 Pro、GLM、DeepSeek 等）——含免费 benefit 额度通道与每日签到。

> [!TIP]
> CodeArts 免费通道有 **5 并发限制**。推荐使用 OpenCode、pi、hermes、openclaw 等能收敛并发度的智能体接入；并发过高的工具会持续触发上游限流，影响体验。

---

## 特性一览

- **双协议端点**：`/v1/chat/completions`（OpenAI）+ `/v1/messages`（Anthropic Messages），Claude Code 零改造接入
- **双通道模型体系**：`agent`（老套餐）与 `benefit`（免费额度）两条上游通道自动区分签名，云端新增免费模型无需改代码
- **本地退避重试**：上游 429/504 等瞬时错误自动重试（首发 + 7 次，总退避约 126s，与客户端超时窗口对齐），连续失败才返回 502，不打断智能体会话
- **prompt cache 模拟**：为不支持缓存的模型生成 Anthropic 风格 `cache_read` usage 统计
- **管理后台**：`/admin` React 界面——概览、模型、额度签到、使用日志（每请求 token/耗时明细）、配置热更新
- **使用日志**：JSONL 落盘 + 内存聚合，时间区间筛选、每日统计、按模型分组、分页明细（token / 耗时 / 费用 / Credits 估算）
- **每日签到**：`benefit.py` 一键领取免费额度，支持计划任务自动化
- **本地 API Key 鉴权**：`/v1/*` 默认强制校验 `x-api-key` / `Authorization: Bearer`，key 在管理后台「API Key」页签自助生成与启停（鉴权在设置 `ADMIN_PASSWORD` 后真正生效，见下文说明）
- **默认仅绑定 127.0.0.1**，不主动对外网开放（可用 `CODEARTS_PROXY_HOST` 覆盖，需自备 TLS 与网络边界）

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置密钥

复制 `config.example.json` 为 `config.json`（或直接创建），填入：

```json
{
  "CODEARTS_AK": "你的AccessKey",
  "CODEARTS_SK": "你的SecretKey",
  "ADMIN_PASSWORD": "管理界面口令"
}
```

`ADMIN_PASSWORD` 可选，设置后启用 `/admin` 后台（也可由 `./start.sh` 首次启动时交互引导设置）。**未设置时管理后台整体停用，`/v1/*` 的 API Key 鉴权也一并放行**——此时没有生成 key 的入口，需要鉴权请务必设置口令。

**如何获取 AK/SK**：打开 <https://console.huaweicloud.com/iam/#/mine/accessKey> → 登录 → 新增访问密钥  →  新增好了之后会弹窗，点击弹窗的下载按钮  →  打开下载的csv文档。

> [!WARNING]
> AK/SK 具有华为云账号最高访问权限，请勿泄露。

### 3. 启动

```bash
./start.sh        # 推荐：自动检查 config.json，首次启动引导设置 admin 口令
```

### 4. 接入智能体

首次使用先打开 <http://127.0.0.1:51818/admin>，在「API Key」页签生成一个 key（生成后明文在列表中可随时查看与复制）。

**OpenAI 兼容工具**（OpenCode / Cline / Continue / LobeChat / Cherry Studio 等）：

| 配置项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:51818/v1` |
| API Key | 在管理后台「API Key」页签生成的 key（`sk-` 开头） |
| 模型 | `openpangu-2.0-pro` |

**Claude Code**（Anthropic Messages 协议）：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:51818 \
ANTHROPIC_AUTH_TOKEN='<在管理后台生成的 API Key>' \
ANTHROPIC_MODEL='claude-sonnet-4-5[4m]' \
ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5 \
claude
```

设置 `ADMIN_PASSWORD` 后，不带 key 的请求返回 401，被禁用的 key 返回 403；key 会在重启后保留（存于 `api-keys.json`，0600）。若在配置页把 `CODEARTS_REQUIRE_API_KEY` 置 `0`，则临时关闭鉴权（仅建议在完全可信的本机环境使用）；未设置 `ADMIN_PASSWORD` 时管理面禁用，鉴权同样放行。

默认模型映射：`claude-sonnet-4-5 → openpangu-2.0-pro`（主工作模型）、`claude-haiku-4-5 → glm-5.3-flash`（免费 benefit 模型，后台小任务不耗套餐额度）。工具调用（tool_use / tool_result）双向转换，Claude Code 本地工具可正常回环。

**思考文本化**：Claude Code 客户端（UA 匹配 `claude-cli` / `claude-code`）请求时，上游 `reasoning_content` 不再以原生 thinking 块返回，而是改写为灰色文本内联展示——流式逐行输出 `💭 Thinking` 头行，每段结束追加 `💭 Thought for Xs` 并补一个尾随换行；非流式返回独立 text 块。多轮对话中已渲染的思考文本在转发上游前剥离。由 `CODEARTS_THINKING_AS_TEXT` 控制（默认开启，可在管理后台配置页切换），关闭后恢复原生 thinking 块，但历史剥离仍生效。

---

## 每日签到（免费额度）

```bash
python benefit.py           # 签到 + 查询余额
python benefit.py status    # 只查签到状态
python benefit.py claim     # 只签到
python benefit.py models    # 查询免费模型列表
```

Windows 计划任务每日自动签到（09:05）：

```cmd
schtasks /create /tn CodeArtsBenefit /tr "python C:\...\benefit.py claim" /sc daily /st 09:05
```

也可以在 `/admin` 后台的「额度」页一键签到。

---

## 管理后台

浏览器打开 <http://127.0.0.1:51818/admin>（需 `config.json` 设置 `ADMIN_PASSWORD`）：

| 页签 | 功能 |
|---|---|
| 概览 | 监听地址、上游、模型数、缓存模拟开关状态 |
| 模型 | 双通道模型列表、强制刷新云端同步 |
| 额度 | 免费额度余额、一键签到；积分用量（总量/已用/剩余 + 进度条 + 刷新） |
| 使用日志 | 时间区间与每日统计切换、四张统计卡（请求数 / 本页费用 / Credits / Token + 缓存占比环形）、按模型分组（可折叠）、8 列请求明细（时间 / IP / 账号 / 模型 / TOKEN 用量 / 用时 / 费用 / KIRO CREDITS），默认每页 100 条（API 上限 200） |
| API Key | 生成（展示明文并提供复制）、改名、启停、删除；显示强制鉴权开关状态，关闭时给出醒目提示 |
| 配置 | 模型映射、缓存模拟开关、思考文本化开关、API Key 强制鉴权开关、账号显示名、模型单价表、Credits 换算系数热更新（写回 config.json）；密钥脱敏展示，`CODEARTS_SNAP_SESSION` 可在页面上更新（提交掩码或纯 `*` 串不会覆盖真实值） |

### 积分用量与 `CODEARTS_SNAP_SESSION`

「额度」页的**积分用量**卡片数据来自华为云工作台积分接口，凭据不是 AK/SK 签名，而是工作台 Cookie 中的 `devclouddevuibjJ_SESSION_ID` 值：

1. 浏览器登录华为云 CodeArts 工作台，从 Cookie 中复制 `devclouddevuibjJ_SESSION_ID` 的值
2. 写入 `config.json` 的 `CODEARTS_SNAP_SESSION`（`config.example.json` 已含此键；也可在 `/admin` 配置页更新，无需重启）
3. 在「额度」页点击「刷新」查看积分总量 / 已用 / 剩余

### 前端构建（admin-ui/）

管理后台前端为 React 18 预构建产物（`admin-ui/dist/` 已入库，运行 server 零 Node 依赖）。修改前端代码后需重新构建：

```bash
cd admin-ui
npm install       # 首次或依赖变更时
npm run build     # 产出 dist/app.js + dist/app.css（含 index.html 壳）
```

session 过期时卡片显示「积分会话已过期，请更新 CODEARTS_SNAP_SESSION」，按步骤 1-2 重新获取即可。

> 前提：`ADMIN_PASSWORD` 未配置时管理后台整体未启用，积分卡片与 session 更新入口不可用，此时只能手改 `config.json`。session 值仅存于本机 `config.json`（0600 权限），不会写入代码、日志或错误信息。

---

## 配置项（config.json）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `CODEARTS_AK` / `CODEARTS_SK` | 必填 | 华为云访问密钥 |
| `ADMIN_PASSWORD` | 空 | 设置后启用 `/admin` 管理界面 |
| `ADMIN_SECRET_KEY` | 自动生成 | admin 会话签名密钥 |
| `CODEARTS_REQUIRE_API_KEY` | `1` | 是否强制 `/v1/*` 校验 API Key；`0` 关闭（仅建议本机可信环境）。仅在已设置 `ADMIN_PASSWORD` 时生效 |
| `CODEARTS_PROXY_HOST` / `CODEARTS_PROXY_PORT` | `127.0.0.1` / `51818` | 监听地址与端口 |
| `CODEARTS_ANTHROPIC_MODEL_MAP` | 内置默认表 | Claude 别名映射，格式 `alias:model` 逗号分隔，设置后覆盖默认表 |
| `CODEARTS_CACHE_SIM` | `1` | prompt cache 模拟开关（`0` 关闭） |
| `CODEARTS_NO_OPEN` | 空 | 设为 `1` 时启动不自动打开浏览器 |
| `CODEARTS_SNAP_SESSION` | 空 | 华为云工作台积分接口会话（Cookie `devclouddevuibjJ_SESSION_ID` 的值），配置后「额度」页显示积分用量；过期可在 `/admin` 配置页更新 |
| `CODEARTS_ACCOUNT_NAME` | `主账号` | 使用日志页「账号」列显示名；留空回退「主账号」 |
| `CODEARTS_PRICING` | `{}` | 模型单价表，JSON 对象：`{"<上游模型 id>": {"input": 0.1, "output": 0.3, "cache_read": 0.02, "cache_creation": 0.1}}`，单位 USD / 1M tokens；`input` / `output` 必填，缓存单价缺省按 `input` |
| `CODEARTS_CREDITS_RATE` | `1.0` | Credits 换算系数（Credits = 费用 × 系数） |
| `CODEARTS_THINKING_AS_TEXT` | `1` | Claude Code 客户端的思考内容文本化开关（`0` 关闭，恢复原生 thinking 块；历史剥离不受此开关控制） |

### 费用与 Credits 估算口径

上游 CodeArts 响应不含计费字段，使用日志中的费用、Credits 与缓存节省额均为本地估算：

- 费用 = Σ(token × 单价) ÷ 1,000,000，单价由 `CODEARTS_PRICING` 按上游模型 id 配置（USD / 1M tokens）
- Credits = 费用 × `CODEARTS_CREDITS_RATE`（默认 1.0）
- 缓存节省额 = 缓存读取 token × (输入单价 − 缓存读取单价) ÷ 1,000,000，负值截断为 0
- `cache_read` / `cache_creation` 单价缺省时按 `input` 单价计算
- 未配置单价的模型费用与 Credits 显示「–」，不猜测价格
- 缓存 token 由 `cache_sim.py` 在本地模拟（仅 Anthropic 端点），费用与节省额是估算值，不代表上游真实计费
- 历史记录按当前单价表实时计算，调整单价后历史费用会随之变化

---

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest               # 全量单元测试
python test_openai.py          # 集成冒烟（需先启动 server 且已生成 API Key）
python test_openai.py --key sk-xxx   # 显式指定 key
```

`test_openai.py` 缺省读取 `api-keys.json` 中首个启用的 key。

---

## 工作原理

```
智能体（Claude Code / OpenCode / ...）
        │  OpenAI 或 Anthropic 协议
        ▼
┌─────────────────────────────────────┐
│  Flask 代理 (127.0.0.1:51818)        │
│  ├─ anthropic_proxy  协议转换+流式   │
│  ├─ cache_sim        缓存 usage 模拟 │
│  ├─ api_keys         API Key 鉴权    │
│  ├─ usage_log        使用日志        │
│  ├─ admin_ui         管理后台        │
│  └─ SDK-HMAC-SHA256 签名            │
│      ├─ agent 通道：基础签名         │
│      └─ benefit 通道：+3 个福利头    │
└─────────────────────────────────────┘
        │  HTTPS
        ▼
  华为云 CodeArts（chat/completions 上游 + opengw 签到/模型发现）
```

- **模型列表生命周期**：启动云端同步 → 原子写缓存 `models-cache.json` → 300 秒 TTL 增量刷新；回退链为 云端 → 缓存 → 硬编码兜底
- **上游容错**：429/500/502/503/504 本地退避重试 7 次（2/4/8/16/32 + 32s×2，总退避约 126s），耗尽才把 502 交客户端
- **流式怪癖修补**：benefit 流缺 `finish_reason` 时自动补块、`content: null` 改写为空串、`finish_reason: "other"` 归一化为 `"stop"`

---

## 代码结构

```text
codearts2cc/
├── server.py                  # 主服务：签名 + 转发（OpenAI 端点 + 重试）
├── anthropic_proxy.py         # Anthropic Messages 端点：协议转换 + 流式状态机
├── cache_sim.py               # prompt cache usage 模拟
├── usage_log.py               # 使用日志（JSONL + 聚合）
├── pricing.py                 # 模型单价表与费用 / Credits 估算
├── api_keys.py                # API Key 存储与校验（api-keys.json，0600）
├── admin_ui.py                # 管理后台（API + 服务 React 预构建产物）
├── admin_api_keys.py          # 管理后台 API Key 路由（蓝图）
├── benefit.py                 # 免费额度签到/查询工具（独立运行）
├── start.sh / stop.sh         # 启动 / 停止脚本
├── models-cache.json          # 模型列表缓存（运行时生成）
├── api-keys.json              # API Key 存储（运行时生成，0600，不入库）
├── usage-log.jsonl            # 使用日志落盘（运行时生成）
├── test_*.py                  # 单元测试（78 例）+ 集成冒烟
└── openspec/                  # 规格与变更记录
```

---

## 说明

- 代理只做签名和转发，不修改模型行为
- 每次调用消耗华为云 CodeArts 套餐额度（token）
- 默认仅绑定 `127.0.0.1`。若通过 `CODEARTS_PROXY_HOST` 改为对外网卡监听，必须自行提供 TLS 终止与网络边界防护（防火墙 / 反向代理 / 访问控制）——内置 API Key 鉴权只是应用层门禁，不能替代传输加密
- API Key 明文仅存于本机 `api-keys.json`（0600，已 gitignore），不写入日志与异常输出
- 本项目及作者与华为云平台及其附属公司无任何关系，华为云平台并未对本项目调用方式明确提出允许；因使用本工具造成的封号/限流等情况与项目作者无关。使用本项目即视为同意以上说明
