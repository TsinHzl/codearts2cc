# 设计：.env 迁移到 config.json

日期：2026-10-17
状态：已确认（用户逐节确认，跳过 OpenSpec，选择「完全替换 + 仅 config.json + 集中配置模块 + start.sh 保留口令门」）

## 1. 目标与范围

把现有 `.env`（dotenv 格式）配置整体替换为 `config.json`，**不保留任何 .env 回退**：

- 启动配置来源只剩 `config.json`；移除 `python-dotenv` 依赖
- shell 环境变量不再参与配置（已设置的会被忽略，文档明示）
- admin 配置页热更新语义不变（改映射/缓存开关立即生效）
- `start.sh` 保留「首次启动引导设置 admin 口令并写入配置文件」体验

## 2. config.json 文件格式

```json
{
  "CODEARTS_AK": "...",
  "CODEARTS_SK": "...",
  "ADMIN_PASSWORD": "...",
  "ADMIN_SECRET_KEY": "",
  "CODEARTS_PROXY_HOST": "127.0.0.1",
  "CODEARTS_PROXY_PORT": 51818,
  "CODEARTS_ANTHROPIC_MODEL_MAP": "claude-sonnet-4-5:openpangu-2.0-pro,claude-haiku-4-5:glm-5.3-flash",
  "CODEARTS_CACHE_SIM": "1",
  "CODEARTS_NO_OPEN": ""
}
```

约定：

- 扁平「键 → 值」结构，键名沿用现有环境变量名（`anthropic_proxy` / `cache_sim` 的读取点改名即可，语义不变）
- 值统一字符串；`CODEARTS_PROXY_PORT` 容忍数字（读取时 `str()` 归一）
- 未写入的键使用代码内默认值（与现状一致）
- 权限：首次创建 `chmod 600`（含 AK/SK/口令明文）
- `.gitignore` 增加 `config.json`（替换原 `.env` 条目）

## 3. 集中配置模块 config_store.py（新文件）

唯一读写入口，约 80 行：

```python
# config_store.py
_path = ""          # server.py 启动时 init() 注入绝对路径
_lock = threading.Lock()
_data = {}

def init(path) -> None            # 加载 JSON → _data；文件不存在视为空 dict（不自动创建）
def get(key, default="") -> str   # _data.get；值归一为 str（int 端口兼容）
def set_many(mapping: dict)       # 加锁 → 更新 _data → 原子写（tmp + os.replace + copymode）
def as_dict() -> dict             # admin 配置页读取全量
```

- 原子写沿用 admin_ui 现有模式（tmp + `os.replace` + `shutil.copymode`）
- 启动时 `os.chmod(path, 0o600)` 兜底收紧权限
- JSON 解析失败 → 打印明确错误并 `SystemExit`（配置文件损坏宁可拒绝启动，不给静默默认值）

## 4. 各模块接入改造

| 文件 | 改造 |
|---|---|
| `server.py` | 删 `load_dotenv`；`config_store.init(CONFIG_PATH)` 后 `AK = config_store.get("CODEARTS_AK")` 等替换全部 `os.getenv`；`ENV_PATH` 改名 `CONFIG_PATH`（指向 `config.json`）；requirements.txt 删 `python-dotenv` |
| `benefit.py` | 独立运行，自己 `config_store.init()` + `get()`（与 server 同库，共享一份实现） |
| `anthropic_proxy.py` | `resolve_model` 内 `os.getenv("CODEARTS_ANTHROPIC_MODEL_MAP")` → `config_store.get(...)` |
| `cache_sim.py` | `cache_enabled()` 同上 |
| `admin_ui.py` | 删 `_read_env_lines`/`_parse_env_line`/`_read_env_map`/`_write_env_value`（约 60 行换 `config_store.as_dict()`/`set_many()`）；`configure()` 参数 `env_file` → `config_path`（仅提示文案用）；PUT 校验不变（白名单键、单行可打印串——JSON 下换行注入已无键行伪造风险，但保留校验防御值污染） |
| `start.sh` | `.env` 检查改 `config.json`；口令门用 `python3 -c "import config_store; ..."` 检查/写入 ADMIN_PASSWORD（`set_many`），`chmod 600` 由 config_store 统一处理 |
| `.env.example` | 删除，改 `config.example.json`（相同键集，值留空/默认） |

- `admin_ui` PUT 流程：校验 → `config_store.set_many(edits)` → 返回 ok（热更新生效由读取点 `get()` 直读保证）
- `server.py:38` 缺 AK/SK 报错文案改为「请在 config.json 中配置」

## 5. 错误处理

- `config.json` 不存在 → 各键走默认值；server 缺 AK/SK 时现有 `SystemExit` 文案兜底
- JSON 语法错误 → `SystemExit("config.json 解析失败: ...")`
- `set_many` 写失败（磁盘/权限）→ 异常向上抛，admin 返回 502（与现行为一致）
- 并发：`_lock` 串行化读-改-写；读取不加锁（dict 读原子）

## 6. 测试

- 新增 `test_config_store.py`：init 缺文件/get 默认值与 int 归一/set_many 原子写回与权限/损坏 JSON 报错
- `test_admin_ui.py`：env_file fixture 改为临时 `config.json`（`configure(config_path=...)`）；换行注入回归用例改为「值含换行必须被拒」语义保留
- 全量 `python -m pytest` 通过；`test_openai.py` 冒烟不变

## 7. 非目标

- 不做 .env → config.json 自动迁移（用户明确选择完全替换）
- 不做配置 schema 校验/类型化配置对象
- 不改 admin 配置页前端交互（仅后端读写实现替换）
