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
- **管理后台**：`/admin` 单文件界面——概览、模型、额度签到、使用日志（每请求 token/耗时明细）、配置热更新
- **使用日志**：JSONL 落盘 + 内存聚合，按模型分组统计、分页明细、首字/总耗时
- **每日签到**：`benefit.py` 一键领取免费额度，支持计划任务自动化
- **仅绑定 127.0.0.1**，不对外网开放

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

`ADMIN_PASSWORD` 可选，设置后启用 `/admin` 后台（也可由 `./start.sh` 首次启动时交互引导设置）。

**如何获取 AK/SK**：打开 <https://console.huaweicloud.com/iam/#/mine/accessKey> → 登录 → 新增访问密钥  →  新增好了之后会弹窗，点击弹窗的下载按钮  →  打开下载的csv文档。

> [!WARNING]
> AK/SK 具有华为云账号最高访问权限，请勿泄露。

### 3. 启动

```bash
./start.sh        # 推荐：自动检查 config.json，首次启动引导设置 admin 口令
```

### 4. 接入智能体

**OpenAI 兼容工具**（OpenCode / Cline / Continue / LobeChat / Cherry Studio 等）：

| 配置项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:51818/v1` |
| API Key | 任意非空字符串（如 `codearts`） |
| 模型 | `openpangu-2.0-pro` |

**Claude Code**（Anthropic Messages 协议）：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:51818 \
ANTHROPIC_AUTH_TOKEN=codearts \
ANTHROPIC_MODEL='claude-sonnet-4-5[4m]' \
ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5 \
claude
```

默认模型映射：`claude-sonnet-4-5 → openpangu-2.0-pro`（主工作模型）、`claude-haiku-4-5 → glm-5.3-flash`（免费 benefit 模型，后台小任务不耗套餐额度）。工具调用（tool_use / tool_result）双向转换，Claude Code 本地工具可正常回环。

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
| 使用日志 | 总量统计、按模型分组、分页请求明细（输入/输出/缓存读取 tokens、首字/总耗时） |
| 配置 | 模型映射、缓存模拟开关热更新（写回 config.json）；密钥脱敏展示，`CODEARTS_SNAP_SESSION` 可在页面上更新（提交掩码或纯 `*` 串不会覆盖真实值） |

### 积分用量与 `CODEARTS_SNAP_SESSION`

「额度」页的**积分用量**卡片数据来自华为云工作台积分接口，凭据不是 AK/SK 签名，而是工作台 Cookie 中的 `devclouddevuibjJ_SESSION_ID` 值：

1. 浏览器登录华为云 CodeArts 工作台，从 Cookie 中复制 `devclouddevuibjJ_SESSION_ID` 的值
2. 写入 `config.json` 的 `CODEARTS_SNAP_SESSION`（`config.example.json` 已含此键；也可在 `/admin` 配置页更新，无需重启）
3. 在「额度」页点击「刷新」查看积分总量 / 已用 / 剩余

session 过期时卡片显示「积分会话已过期，请更新 CODEARTS_SNAP_SESSION」，按步骤 1-2 重新获取即可。

> 前提：`ADMIN_PASSWORD` 未配置时管理后台整体未启用，积分卡片与 session 更新入口不可用，此时只能手改 `config.json`。session 值仅存于本机 `config.json`（0600 权限），不会写入代码、日志或错误信息。

---

## 配置项（config.json）

| 配置项 | 默认 | 说明 |
|---|---|---|
| `CODEARTS_AK` / `CODEARTS_SK` | 必填 | 华为云访问密钥 |
| `ADMIN_PASSWORD` | 空 | 设置后启用 `/admin` 管理界面 |
| `ADMIN_SECRET_KEY` | 自动生成 | admin 会话签名密钥 |
| `CODEARTS_PROXY_HOST` / `CODEARTS_PROXY_PORT` | `127.0.0.1` / `51818` | 监听地址与端口 |
| `CODEARTS_ANTHROPIC_MODEL_MAP` | 内置默认表 | Claude 别名映射，格式 `alias:model` 逗号分隔，设置后覆盖默认表 |
| `CODEARTS_CACHE_SIM` | `1` | prompt cache 模拟开关（`0` 关闭） |
| `CODEARTS_NO_OPEN` | 空 | 设为 `1` 时启动不自动打开浏览器 |
| `CODEARTS_SNAP_SESSION` | 空 | 华为云工作台积分接口会话（Cookie `devclouddevuibjJ_SESSION_ID` 的值），配置后「额度」页显示积分用量；过期可在 `/admin` 配置页更新 |

---

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest               # 全量单元测试（78 例）
python test_openai.py          # 集成冒烟（需先启动 server）
```

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
├── admin_ui.py                # 管理后台（单文件前端 + API）
├── benefit.py                 # 免费额度签到/查询工具（独立运行）
├── start.sh / stop.sh         # 启动 / 停止脚本
├── models-cache.json          # 模型列表缓存（运行时生成）
├── usage-log.jsonl            # 使用日志落盘（运行时生成）
├── test_*.py                  # 单元测试（78 例）+ 集成冒烟
└── openspec/                  # 规格与变更记录
```

---

## 说明

- 代理只做签名和转发，不修改模型行为
- 每次调用消耗华为云 CodeArts 套餐额度（token）
- 仅绑定 `127.0.0.1`，不会对外网开放
- 本项目及作者与华为云平台及其附属公司无任何关系，华为云平台并未对本项目调用方式明确提出允许；因使用本工具造成的封号/限流等情况与项目作者无关。使用本项目即视为同意以上说明
