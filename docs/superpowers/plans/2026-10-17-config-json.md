# config.json 替换 .env 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `.env`（dotenv）配置整体替换为 `config.json`，通过集中配置模块 `config_store.py` 提供读写与热更新，移除 `python-dotenv` 依赖，不保留 .env 回退。

**Architecture:** 新增唯一读写入口 `config_store.py`（init/get/set_many/as_dict，原子写 + 线程锁）；server/benefit/anthropic_proxy/cache_sim/admin_ui 全部从 `os.getenv` 改为 `config_store.get`；admin 配置页后端删掉 .env 按行编辑逻辑改调 `set_many`；`start.sh` 口令门用内联 python 检查/写入 config.json。

**Tech Stack:** Python 3 标准库（json / threading / os / shutil）；pytest；Flask（现有）。

## Global Constraints

- 配置来源仅 `config.json`，无 .env 回退、无 shell 环境变量覆盖（spec §1）
- 配置文件路径：仓库根 `config.json`，与 server.py 同目录（spec §2）
- 值统一字符串；`CODEARTS_PROXY_PORT` 读取时 `str()` 归一（spec §2）
- 文件创建/写回后权限 `0600`（spec §2）
- JSON 解析失败 → `SystemExit`，带「config.json 解析失败」字样（spec §5）
- 所有用户可见文案简体中文；commit 中文正文 + `Co-Authored-By: Claude <noreply@anthropic.com>`
- 测试命令：`python3 -m pytest`（本机无 `python` 别名）

---

### Task 1: config_store 模块（TDD）

**Files:**
- Create: `config_store.py`
- Create: `test_config_store.py`

**Interfaces:**
- Produces:
  - `init(path: str) -> None` — 加载 JSON 到内存 `_data`；文件不存在时 `_data = {}` 不报错；解析失败 `SystemExit("config.json 解析失败: ...")`；成功后 `os.chmod(path, 0o600)`（文件存在时）
  - `get(key: str, default: str = "") -> str` — 返回 `str(_data.get(key, default))`（int 值归一为字符串）
  - `set_many(mapping: dict) -> None` — 加锁更新 `_data` 后原子写回（tmp + `os.replace` + `shutil.copymode`）；写前 `os.chmod(tmp, 0o600)`
  - `as_dict() -> dict` — 返回 `_data` 浅拷贝

- [ ] **Step 1: 写失败测试**

```python
# test_config_store.py
# -*- coding: utf-8 -*-
"""config_store 单元测试：加载 / get 归一 / set_many 原子写 / 损坏 JSON 报错。"""
import json
import os
import stat

import pytest

import config_store


@pytest.fixture(autouse=True)
def _reset():
    """每个用例前重置模块态，避免用例间串扰。"""
    config_store._data = {}
    config_store._path = ""
    yield


def test_init_missing_file_empty(tmp_path):
    config_store.init(str(tmp_path / "config.json"))
    assert config_store.get("CODEARTS_AK") == ""
    assert config_store.get("CODEARTS_AK", "dft") == "dft"


def test_init_loads_json(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"CODEARTS_AK": "ak1", "CODEARTS_PROXY_PORT": 51818}', encoding="utf-8")
    config_store.init(str(p))
    assert config_store.get("CODEARTS_AK") == "ak1"
    assert config_store.get("CODEARTS_PROXY_PORT") == "51818"  # int 归一为 str


def test_init_broken_json_exits(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{broken", encoding="utf-8")
    with pytest.raises(SystemExit, match="config.json 解析失败"):
        config_store.init(str(p))


def test_set_many_atomic_write(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"A": "1"}', encoding="utf-8")
    config_store.init(str(p))
    config_store.set_many({"A": "2", "B": "3"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"A": "2", "B": "3"}
    assert config_store.get("B") == "3"
    assert not os.path.exists(str(p) + ".tmp")  # 原子替换后 tmp 不残留


def test_set_many_preserves_mode(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"A": "1"}', encoding="utf-8")
    os.chmod(p, 0o600)
    config_store.init(str(p))
    config_store.set_many({"A": "9"})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_as_dict_snapshot(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"A": "1"}', encoding="utf-8")
    config_store.init(str(p))
    d = config_store.as_dict()
    d["A"] = "mutated"
    assert config_store.get("A") == "1"  # 浅拷贝，外部修改不影响内部态
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest test_config_store.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'config_store'`）

- [ ] **Step 3: 最小实现**

```python
# config_store.py
# -*- coding: utf-8 -*-
"""集中配置模块：config.json 的唯一读写入口。

取代 .env + python-dotenv：server / benefit / anthropic_proxy / cache_sim /
admin_ui 全部经此读取配置；admin 配置页热更新走 set_many（原子写回）。
文件不存在视为空配置（各键走代码内默认值）；JSON 损坏直接拒绝启动。
"""
import json
import os
import shutil
import threading

_path = ""          # config.json 绝对路径（init 注入）
_data = {}          # 键 → 值（值统一按字符串读出）
_lock = threading.Lock()  # set_many 读-改-写互斥（Flask threaded=True）


def init(path: str) -> None:
    """加载 config.json 到内存；文件不存在视为空配置，损坏则拒绝启动。"""
    global _path, _data
    _path = path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            _data = json.load(handle)
    except FileNotFoundError:
        _data = {}
    except json.JSONDecodeError as error:
        raise SystemExit(f"config.json 解析失败: {error}")
    if not isinstance(_data, dict):
        raise SystemExit("config.json 解析失败: 顶层必须是 JSON 对象")
    if os.path.exists(path):
        os.chmod(path, 0o600)  # 含 AK/SK/口令明文，兜底收紧权限


def get(key: str, default: str = "") -> str:
    """读取配置值；统一归一为 str（容忍 JSON 中写成数字的端口等）。"""
    value = _data.get(key, default)
    return default if value is None else str(value)


def set_many(mapping: dict) -> None:
    """批量更新并原子写回（tmp + os.replace，保留原文件权限）。"""
    global _data
    with _lock:
        _data = dict(_data)
        _data.update(mapping)
        tmp_path = _path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(_data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if os.path.exists(_path):
            shutil.copymode(_path, tmp_path)
        else:
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, _path)


def as_dict() -> dict:
    """返回全量配置浅拷贝（admin 配置页读取）。"""
    return dict(_data)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m pytest test_config_store.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add config_store.py test_config_store.py
git commit -m "feat: 新增 config_store 集中配置模块（config.json 读写 + 原子写回）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: server.py / benefit.py 接入 config_store

**Files:**
- Modify: `server.py:28-38`（dotenv → config_store）、`server.py:72-81`、`server.py:77-78`、`server.py:1010`、`server.py:970`（env_file 参数）
- Modify: `benefit.py:31-36`
- Modify: `requirements.txt`（删 python-dotenv）
- Modify: `.gitignore`（.env → config.json）

**Interfaces:**
- Consumes: Task 1 的 `config_store.init/get`
- Produces: `server.CONFIG_PATH`（config.json 绝对路径，Task 3 configure 注入用）；server 与 benefit 启动即完成 `init()`

- [ ] **Step 1: 修改 server.py 配置加载块**

`server.py:28-38` 改为：

```python
import config_store

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
config_store.init(CONFIG_PATH)

AK = config_store.get("CODEARTS_AK").strip()
SK = config_store.get("CODEARTS_SK").strip()
if not AK or not SK:
    raise SystemExit("缺少 CODEARTS_AK / CODEARTS_SK，请在 config.json 中配置后重试")
```

同时删除 `from dotenv import load_dotenv` 与 `load_dotenv()`（:28、:33）。

- [ ] **Step 2: 修改 server.py 其余 getenv**

```python
# :72-73
LISTEN_HOST = config_store.get("CODEARTS_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(config_store.get("CODEARTS_PROXY_PORT", "51818"))
# :80-81
ADMIN_PASSWORD = config_store.get("ADMIN_PASSWORD").strip()
ADMIN_SECRET_KEY = config_store.get("ADMIN_SECRET_KEY").strip()
# :1010（CODEARTS_NO_OPEN）
if ADMIN_PASSWORD and config_store.get("CODEARTS_NO_OPEN") != "1":
```

`:77-78` 注释与 `ENV_PATH` 改名：

```python
# config.json 绝对路径（admin 配置编辑写回此文件）
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
```

（init 调用放在此处之前，即 Step 1 的位置。）

`:970` admin_ui.configure 参数 `env_file=ENV_PATH` → `config_path=CONFIG_PATH`。

模块 docstring `:13-14` 端口说明不变；如 docstring 提及 .env 则同步改为 config.json。

- [ ] **Step 3: 修改 benefit.py**

`:31-36` 改为：

```python
import config_store

config_store.init(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"))

AK = config_store.get("CODEARTS_AK").strip()
SK = config_store.get("CODEARTS_SK").strip()
```

删除 `from dotenv import load_dotenv` 与 `load_dotenv(...)`；docstring 中 `.env` 字样改 `config.json`。

- [ ] **Step 4: 依赖与忽略规则**

- `requirements.txt` 删除 `python-dotenv>=1.0.0` 行
- `.gitignore`：`/.env` 行替换为 `/config.json`（连同 `.env.tmp`/`config.json.tmp` 如有）

- [ ] **Step 5: 运行验证**

Run: `python3 -m pytest test_config_store.py test_admin_ui.py -q`（admin_ui 未改，允许此步失败项仅来自 admin_ui 对 `env_path` 的引用——Task 3 修复；本步只需确认 server/benefit 无语法错误）
Run: `python3 -c "import ast; ast.parse(open('server.py').read()); ast.parse(open('benefit.py').read())"`
Expected: 语法解析通过

- [ ] **Step 6: Commit**

```bash
git add server.py benefit.py requirements.txt .gitignore
git commit -m "feat: server/benefit 接入 config_store，移除 python-dotenv

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: anthropic_proxy / cache_sim / admin_ui 接入 + admin 配置页重写

**Files:**
- Modify: `anthropic_proxy.py:200`
- Modify: `cache_sim.py:32`
- Modify: `admin_ui.py:274-382`（配置编辑整段）、`admin_ui.py:41,80`（env_path）、`admin_ui.py:64`（configure 签名）、`admin_ui.py:142,379`（文案）
- Modify: `test_admin_ui.py`（fixture 与断言）

**Interfaces:**
- Consumes: `config_store.get/as_dict/set_many`（Task 1）；`server.CONFIG_PATH`（Task 2）
- Produces: `admin_ui.configure(..., config_path=...)` 新签名；`/api/admin/config` GET/PUT 行为不变（对外契约不变）

- [ ] **Step 1: anthropic_proxy.py + cache_sim.py**

```python
# anthropic_proxy.py:200
env_map = config_store.get("CODEARTS_ANTHROPIC_MODEL_MAP").strip()
# cache_sim.py:32
return config_store.get("CODEARTS_CACHE_SIM", "1").strip() != "0"
```

两文件头部加 `import config_store`。

- [ ] **Step 2: admin_ui.py 删 .env 按行编辑，改调 config_store**

- 删除 `_read_env_lines` / `_parse_env_line` / `_read_env_map` / `_write_env_value`（:286-350）与 `_env_write_lock`
- 全局 `env_path = ""` 改 `config_path = ""`；`configure(..., env_file, ...)` 参数改 `config_path`，赋值同步改名
- `admin_get_config`（:354-358）：

```python
@blueprint.route("/api/admin/config", methods=["GET"])
def admin_get_config():
    env_map = config_store.as_dict()
    editable = {key: env_map.get(key, "") for key in EDITABLE_KEYS}
    secrets = {key: _mask(env_map.get(key, "")) for key in READONLY_SECRET_KEYS}
    return jsonify({"editable": editable, "secrets": secrets})
```

- `admin_set_config` PUT 校验逻辑保留（白名单键 + 单行可打印串），写回改为：

```python
        config_store.set_many({key: value for key, value in edits.items()})
        return jsonify({"ok": True, "updated": dict(edits)})
```

（「键不存在于 .env」检查删除——JSON 模式下任意白名单键均可写入；错误文案 `:379` 相应删除。）
- `:142` 文案改「管理界面未启用：请先在 config.json 设置 ADMIN_PASSWORD」
- 文件头注释 `.env` 字样改 `config.json`

- [ ] **Step 3: 更新 test_admin_ui.py**

- fixture：`env_file="/nonexistent/.env"` → `config_path="/nonexistent/config.json"`；`app_with_pw` 中临时 `.env` 写入改为：

```python
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({
        "CODEARTS_AK": "AK1234567890",
        "CODEARTS_SK": "SKsecret999999",
        "CODEARTS_ANTHROPIC_MODEL_MAP": "claude-sonnet-4-5:openpangu-2.0-pro",
        "CODEARTS_CACHE_SIM": "1",
    }, ensure_ascii=False), encoding="utf-8")
```

且每个用例前 `config_store.init(str(config_file))`（fixture 内调用，autouse 重置态可放 test_config_store 风格——直接在 `app_with_pw` 里 init 即可），`configure(config_path=str(config_file), ...)`。
- `test_edit_map_writes_env` 改名 `test_edit_map_writes_config`，断言改为：

```python
        data = json.loads(open(admin_ui.config_path, encoding="utf-8").read())
        assert data["CODEARTS_AK"] == "AK1234567890"
        assert data["CODEARTS_SK"] == "SKsecret999999"
        assert data["CODEARTS_ANTHROPIC_MODEL_MAP"] == "new-alias:GLM-5.2"
```

（删 `os.environ` 断言。）
- `test_edit_nonexistent_key_rejected` 删除（JSON 模式下白名单键不存在也允许写）或改为「未知键仍拒绝」语义保留：已有 `test_edit_unknown_key_rejected` 覆盖，此用例删除
- `test_edit_multiline_value_rejected` / `test_edit_non_string_value_rejected` 保留（校验仍在），断言「文件未被改动」改为 `json.loads(...)` 后无 `ADMIN_PASSWORD` 键
- 文件头 import 加 `import json, config_store`

- [ ] **Step 4: 运行全量单测**

Run: `python3 -m pytest test_config_store.py test_admin_ui.py test_anthropic_proxy.py test_cache_sim.py -q`
Expected: 全部通过

- [ ] **Step 5: Commit**

```bash
git add anthropic_proxy.py cache_sim.py admin_ui.py test_admin_ui.py
git commit -m "feat: 代理模块与 admin 配置页接入 config_store，删除 .env 按行编辑

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: start.sh 口令门 + config.example.json + 文档同步

**Files:**
- Modify: `start.sh`（.env 检查、口令门、提示文案）
- Delete: `.env.example`；Create: `config.example.json`
- Modify: `README.md`、`CLAUDE.md`（.env → config.json 全部字样）

**Interfaces:**
- Consumes: Task 1-3 完成后的 `config_store`
- Produces: 一条命令 `./start.sh` 可从零完成「无 config.json → 报错提示 → 引导口令 → 启动」

- [ ] **Step 1: config.example.json**

```json
{
  "CODEARTS_AK": "",
  "CODEARTS_SK": "",
  "ADMIN_PASSWORD": "",
  "ADMIN_SECRET_KEY": "",
  "CODEARTS_PROXY_HOST": "127.0.0.1",
  "CODEARTS_PROXY_PORT": "51818",
  "CODEARTS_ANTHROPIC_MODEL_MAP": "",
  "CODEARTS_CACHE_SIM": "1",
  "CODEARTS_NO_OPEN": ""
}
```

- [ ] **Step 2: start.sh 改造**

```bash
if [ ! -f config.json ]; then
    echo "错误: 缺少 config.json，请先执行 cp config.example.json config.json 并填入 CODEARTS_AK/CODEARTS_SK" >&2
    exit 1
fi

# admin 口令门：用内联 python 检查 config.json 中的 ADMIN_PASSWORD
admin_pw_set() {
    python3 -c "import config_store; config_store.init('config.json'); import sys; sys.exit(0 if config_store.get('ADMIN_PASSWORD').strip() else 1)"
}
if ! admin_pw_set; then
    if [ ! -t 0 ]; then
        echo "错误: 未设置 ADMIN_PASSWORD（admin 管理界面口令），且当前为非交互环境无法引导设置" >&2
        echo "请先在 config.json 中写入 ADMIN_PASSWORD 后重试" >&2
        exit 1
    fi
    read_secret() {
        if ! IFS= read -rs -p "$1" "$2"; then
            printf '\n错误: 读取密码输入失败，已取消启动\n' >&2
            exit 1
        fi
        printf '\n'
    }
    echo "检测到尚未设置 admin 管理界面访问密码，请先完成设置（写入 config.json 的 ADMIN_PASSWORD）"
    while true; do
        read_secret "请输入管理界面访问密码: " admin_pw
        read_secret "请再次输入以确认: " admin_pw2
        if [ -z "$admin_pw" ]; then
            echo "密码不能为空，请重新设置" >&2
        elif [ "$admin_pw" != "$admin_pw2" ]; then
            echo "两次输入不一致，请重新设置" >&2
        elif ! printf '%s' "$admin_pw" | grep -Eq '^[A-Za-z0-9_@%+=:,.!?/~^-]+$'; then
            echo "密码仅可包含字母、数字及 _ @ % + = : , . ! ? / ~ ^ -，请重新设置" >&2
        else
            python3 -c "import config_store; config_store.init('config.json'); config_store.set_many({'ADMIN_PASSWORD': '$admin_pw'})"
            echo "管理密码已写入 config.json，启动后访问 /admin 登录"
            break
        fi
    done
fi
```

（原 `.env` grep 检查、`printf >> .env`、`chmod 600 .env` 全部删除；口令经单引号内插存在注入面——实现时改为 `json.dumps` 传参更稳：`python3 -c "..." "$admin_pw"` 配 `sys.argv[1]` + `config_store.set_many({'ADMIN_PASSWORD': sys.argv[1]})`，**必须用 argv 传值，禁止字符串内插**。）

- [ ] **Step 3: 文档同步**

- `README.md`：`.env` → `config.json` 全部 16 处字样（含 `cp .env.example .env` → `cp config.example.json config.json`、环境变量表说明列、「每日签到」节 benefit.py 说明）；`python-dotenv` 如提及则删除
- `CLAUDE.md`：项目概述「AK/SK 在 .env」、常用命令注释、架构节「与 server 共享 .env」等改 config.json

- [ ] **Step 4: 验证**

Run: `bash -n start.sh`
Expected: 语法通过
Run: `python3 -m pytest -q`
Expected: 全量通过

- [ ] **Step 5: Commit**

```bash
git add start.sh config.example.json README.md CLAUDE.md
git rm .env.example
git commit -m "feat: start.sh 口令门改写 config.json，.env.example 替换为 config.example.json

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 端到端验证

**Files:** 无新增（验证任务）

- [ ] **Step 1: 手动冒烟**

```bash
cp config.example.json /tmp/smoke-config.json   # 不污染仓库根
# 仓库根放一个真实 config.json（用户已有 AK/SK，手工填入或复用原 .env 值）
python3 server.py   # 确认启动日志正常、无 dotenv import
python3 test_openai.py   # 需 server 运行中
```

- [ ] **Step 2: 清理确认**

Run: `grep -rn "dotenv\|load_dotenv" --include="*.py" . | grep -v openspec`
Expected: 无输出
Run: `grep -rn "\.env\b" --include="*.py" --include="*.sh" --include="*.md" . | grep -v openspec | grep -v "\.env\.example"`
Expected: 无输出（README/CLAUDE.md/start.sh/py 全部清理）

- [ ] **Step 3: 最终提交（如有遗留）**

```bash
git add -A && git commit -m "chore: .env → config.json 迁移收尾

Co-Authored-By: Claude <noreply@anthropic.com>"
```
