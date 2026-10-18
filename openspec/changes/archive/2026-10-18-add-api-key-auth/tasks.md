# 任务清单：add-api-key-auth

## 状态：ARCHIVED

## 任务

- [x] T1 新建 `api_keys.py` 存储模块：`init(path)` 路径注入 + `list_keys` / `create_key` / `update_key` / `delete_key` / `authenticate`；`api-keys.json` 原子写（**先 `os.open(tmp, ..., 0o600)` 建 tmp，再写入，最后 `os.replace`**——不可 replace 后才 chmod，否则残留 0644 明文窗口）+ 进程内锁；写盘失败回滚内存态并报错；未 init 时读写为无害空操作；损坏或缺失按空列表处理并告警；模块 docstring 记录"引入按 key 用量统计前必须先加 id 计数器"的约束。验证：`python -m pytest test_api_keys.py`（本任务先写测试文件，覆盖增删改查、持久化重载、写盘失败回滚内存态、损坏/缺失/未 init 降级、权限位 0600、`authenticate` 返回三态；明文不落日志 = 用 `capsys` 断言 stdout/stderr 不含 `sk-` 子串）
- [x] T2 `server.py` 挂载 app 级 `before_request` 鉴权门禁：`/v1/*` 前缀 + 非 OPTIONS 才校验；`x-api-key` 优先、`Authorization: Bearer` 次之；key 与候选值统一 `encode("utf-8")` 后 `hmac.compare_digest` 全量遍历（不短路）；缺 key / 无效 key 返回 401（**key 列表为空时消息明确说明"尚未创建任何 API Key"**），禁用 key 返回 403；`CODEARTS_REQUIRE_API_KEY`（默认开启）为 `0` 时整体跳过；未设置 `ADMIN_PASSWORD`（管理面禁用）时整体跳过并在启动日志提示；错误体按 `/v1/messages*` 与非该前缀分流；**同步把 `x-api-key` 加入 `_cors` 的 `Access-Control-Allow-Headers`**（否则浏览器跨域工具的预检被拒）。验证：新增 `test_api_key_gate.py` 逐一覆盖「请求鉴权门禁」的 11 条 Scenario（携带有效 key 放行、Bearer 放行与优先序、缺 key 401 含空列表消息、CORS 预检含 `x-api-key`、无效 key 401、禁用 403、关闭开关放行、非 ASCII 与超长 key 均 401 而非 500、管理面无鉴权、管理面禁用时放行、错误体分流）。**测试骨架注意**：`server.py` 在 import 时缺 AK/SK 会 `raise SystemExit`，需沿用 `test_admin_ui.py` 已有的 `monkeypatch.setattr(config_store, "init"/"get")` 先注入模式（或 `importlib` 前打好 env），不可直接 import 后建 client
- [x] T3 `admin_ui.py` 新增 `/api/admin/api-keys` 四个端点（GET 列表 + 开关状态、POST 创建、PATCH 改名/启停、DELETE 删除），沿用既有口令会话鉴权与 `_json_error` 风格；名称校验（非空字符串、≤64 字符）；`enabled` 必须为布尔；不存在的 id 返回 404。验证：`test_admin_ui.py` 新增 `TestApiKeys` 类覆盖列表/创建/校验失败/改名/启停/删除/404/未登录 401；**同步扩展该文件的 `_reset_global_state` fixture，把新模块 `api_keys` 的内存态与路径一并重置**，避免用例间状态串扰
- [x] T4 把 `CODEARTS_REQUIRE_API_KEY` 接入配置编辑：后端加入 `admin_ui.EDITABLE_KEYS` 与 `_EDITABLE_DEFAULTS`（默认 `"1"`）并加取值校验（仅接受 `"0"` / `"1"`，非法值 400 且不写回）；**前端 `admin-ui/src/tabs/ConfigTab.tsx` 三处同步**——`cfg` 初始 state 增键、`load()` 归一化（照 `!== '0' ? '1' : '0'` 模式）、表单渲染项，否则配置页提交不出该键。验证：`test_admin_ui.py` 新增用例断言默认回显 `1`、提交 `0` 写回 config.json 且生效、提交非法值被拒绝且 config.json 保持原值
- [x] T5 前端新增 `admin-ui/src/tabs/ApiKeyTab.tsx`：列表 + 空状态引导 + 生成对话框（展示明文与复制反馈）+ 改名 + 启停 + 删除确认；`Layout.tsx` 的 `TabKey` 与 `navDefs` 增加 `apikey` 页签（移动端自动落入「更多」抽屉）；`App.tsx` 挂载该页签。验证：`npm --prefix admin-ui run build` 无 TS 错误并产出 dist；桌面与移动端视口手工确认无横向溢出
- [x] T6 同步受影响的既有资产：`test_openai.py` 冒烟脚本改为携带 key（**用脚本参数 `--key`，缺省回退读 `api-keys.json` 首个启用 key**；不引入 `CODEARTS_API_KEY` 环境变量，避免与"所有 `CODEARTS_*` 一律走 config.json"的既有约定冲突）；`.gitignore` 增加 `api-keys.json` 与 `api-keys.json.tmp`（原子写残留含明文，与既有 `config.json.tmp` 同处理）；`config.example.json` 与 `README.md` 环境变量表补登 `CODEARTS_REQUIRE_API_KEY`；`CLAUDE.md`、`openspec/project.md` 与 `README.md` 更新"代理不鉴权 / API Key 填任意非空字符串"的过时描述（README 接入表与 Claude Code 示例会直接误导用户拿到 401），并在 README 标注非回环暴露时需自备 TLS 与网络边界。验证：启动 server 后 `python test_openai.py` 通过；`git check-ignore -v api-keys.json api-keys.json.tmp` 均有命中；`grep -rn "任意非空" README.md CLAUDE.md` 无命中；`config.example.json` 含新键

## 验收标准

- [x] 全新环境（无 `api-keys.json`）启动服务后，`/admin` 可正常打开、口令登录成功、各既有页签功能不变
- [x] 在「API Key」页签生成 key 后，带该 key 的 `/v1/chat/completions` 与 `/v1/messages` 请求正常返回；不带 key 返回 401；被禁用的 key 返回 403
- [x] 401 / 403 错误体分别符合 OpenAI 与 Anthropic 客户端的错误解析格式，客户端能显示可读消息
- [x] 携带非 ASCII 或超长 key 的请求返回 401，服务端不产生 500 与异常堆栈
- [x] 浏览器跨域工具（回环源）带 `x-api-key` 的 `OPTIONS` 预检返回的 `Access-Control-Allow-Headers` 含 `x-api-key`
- [x] 未设置 `ADMIN_PASSWORD` 时，`/v1/*` 无 key 请求放行且启动日志给出提示
- [x] 删除 key 后原 key 立即失效；重启服务后已创建的 key 仍有效
- [x] `api-keys.json` 权限为 0600，且其与其 `.tmp` 残留均不出现在 `git status` 的待提交列表
- [x] 配置页将 `CODEARTS_REQUIRE_API_KEY` 置 `0` 后，无 key 请求立即放行（无需重启）；重新置 `1` 后立即恢复校验；提交其他值被拒绝
- [x] key 明文不出现在任何服务日志与异常输出中
- [x] `python -m pytest test_api_keys.py test_api_key_gate.py test_admin_ui.py` 全绿
- [x] `npm --prefix admin-ui run build` 成功，dist 产物入库（旧 hash 文件删除、新 hash 文件加入）

## 风险与依赖

- 依赖 T1 完成后才能并行推进 T2 / T3；T5 依赖 T3 的接口定型
- T6 会改动既有测试脚本行为，若用户环境未建 key 需先经管理页面创建，属预期的一次性迁移成本
