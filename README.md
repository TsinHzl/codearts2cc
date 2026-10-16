# CodeArts OpenAI 兼容代理

把华为云 CodeArts [https://codearts.huaweicloud.com/] 的模型接口（盘古开源大模型2.0、GLM-5.2）包装成**标准 OpenAI Chat Completions API**。

底层走华为云 `SDK-HMAC-SHA256` 请求签名，AK/SK 存放在 `.env`。
## 新增免费层级模型，可使用benefit.py一键签到并通过server.py调用
> [!TIP]
> 建议使用opencode，pi，hermes，openclaw作为智能体接入。免费codearts具有5并发限制，其他代码智能体通常会超过这个限制，从而影响体验

## 使用方法

### 1. 安装依赖

```cmd
pip install -r requirements.txt
```

### 2. 配置 AK/SK

编辑 `.env`：

```ini
CODEARTS_AK=你的AccessKey
CODEARTS_SK=你的SecretKey
```
#### 如何获取AK/SK(Access Key/Secret Key)
进入 https://console.huaweicloud.com/iam/#/mine/accessKey ，登录后-->新增访问密钥 即可获取

**请勿泄露你的AK/SK，这具有华为云账号的最高访问权限**

### 3. 启动代理

推荐一键启动（自动检查 .env 配置）：

```cmd
./start.sh
```

或直接运行：

```cmd
python server.py
```

默认监听：

```text
http://127.0.0.1:8787
```

### 4. 在各类工具中接入

把 OpenAI 兼容 Base URL 指向：

```text
http://127.0.0.1:8787/v1
```

- API Key：任意非空字符串（例如 `codearts`）
- 模型：`openpangu-2.0-pro`

支持的工具示例：OpenCode、Cline、Continue、LobeChat、Cherry Studio 等所有支持自定义 OpenAI 兼容端点的工具。

### 5. Claude Code 接入（Anthropic Messages 协议）

本代理同时提供 Anthropic Messages 兼容端点 `POST /v1/messages`，Claude Code 可直接把本代理作为模型后端：

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 \
ANTHROPIC_AUTH_TOKEN=codearts \
ANTHROPIC_MODEL='claude-sonnet-4-5[4m]' \
ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5 \
claude
```

- `ANTHROPIC_AUTH_TOKEN`：任意非空字符串
- 默认模型映射：`claude-sonnet-4-5 → openpangu-2.0-pro`（主工作模型）、`claude-haiku-4-5 → glm-5.3-flash`（免费 benefit 模型，Claude Code 后台小任务不耗套餐额度）
- 自定义映射：设置环境变量 `CODEARTS_ANTHROPIC_MODEL_MAP`（格式 `alias:model`，逗号分隔，如 `claude-sonnet-4-5:GLM-5.2,claude-haiku-4-5:glm-5.3-flash`），设置后覆盖默认表
- `claude-*` 未命中映射表时自动兜底到可用模型列表的第一个模型
- 工具调用（tool_use/tool_result）双向转换，Claude Code 本地工具（Bash/Read/Edit 等）可正常回环

## 本地测试

```bash
pip install -r requirements-dev.txt   # 开发依赖（pytest）
python -m pytest test_anthropic_proxy.py   # 协议转换单元测试（16 例）
python test_openai.py                 # OpenAI 端点集成冒烟（需先启动 server）
```

## 接口说明

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 格式聊天补全，支持 `stream` 流式 |
| `POST /v1/messages` | Anthropic Messages 格式聊天补全（Claude Code 接入），支持 `stream` 流式 |
| `POST /v1/messages/count_tokens` | Anthropic token 数本地粗略估算 |
| `GET /v1/models` | 模型列表 |

## 代码结构

```text
codearts2api/
├── .env                       # AK/SK
├── server.py                  # 主服务：签名 + 转发（OpenAI 端点）
├── anthropic_proxy.py         # Anthropic Messages 端点：协议转换 + 流式状态机
├── start.sh                    # 一键启动脚本（.env 前置检查）
├── requirements.txt           # 生产依赖
├── requirements-dev.txt       # 开发依赖（pytest）
├── test_anthropic_proxy.py    # 协议转换单元测试
├── test_openai.py             # 本地测试脚本
└── README.md
```

## 说明

- 代理只做签名和转发，不修改模型行为。
- 每次调用会消耗华为云 CodeArts 套餐额度（token）。
- 仅绑定 `127.0.0.1`，不会对外网开放。
- 本项目及本项目作者与华为云平台及其附属公司无任何关系，且华为云平台中并未对本项目中调用方式明确提出允许，若造成封号/限流等任何因使用本工具造成的情况与项目作者无关。如果您使用该项目，则默认视为同意本Readme中所有说明

# 社区
感谢[LinuxDO](https://linux.do/)对本项目进行推广
