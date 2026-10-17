# 任务清单：redesign-usage-log-page

## 状态：ARCHIVED

## 任务

- [x] **任务 1：记录端补齐 `cache_creation_tokens`** — `anthropic_proxy.py` 的 `_record_usage` 写入 `cache_creation_tokens`（取 cache 模拟的 creation 量）；OpenAI 端点显式写入 `cache_creation_tokens: 0`（不读取上游字段）。验证：`test_anthropic_proxy.py` 新增断言 Anthropic 字段的用例；`test_admin_ui.py` 直调 `server._record_usage` 断言 OpenAI 路径写入 0；全部用例全绿。
- [x] **任务 2：新增 `pricing.py` 计价模块** — 实现 `load_table()`（每查询解析一次，返回解析结果与警告）与 `compute(entry, table, rate)`；校验有限非负实数、拒绝布尔值与 `NaN`/`Infinity`；`input`/`output` 必填、`cache_read`/`cache_creation` 缺省按 `input`；未命中模型返回 `None`，`saved` 负值截断为 0；脏数据按未计价返回 `None`。验证：`test_pricing.py` 覆盖命中/未命中/非法 JSON/负数/`NaN`/`Infinity`/布尔值/缓存单价缺省/节省额截断/上游 id 键/历史缺 `cache_creation_tokens` 按 0 共 10 个用例，全部全绿。
- [x] **任务 3：`usage_log.py` 全量明细与区间/每日聚合** — 删除 `_MAX_ENTRIES` 裁剪，`_entries` 保留全量；新增模块级 `_file_lines` 计数器（按成功解析条数）与运行期滚动（先追加后判定，超 `_MAX_FILE_ENTRIES` 保留较新一半、原子重写、重建 `_summary`/`_by_model`）；`query()` 新增 `start` / `end` / `daily` 参数，返回 `dict(entry)` 副本，`daily` 只返回原始 token 聚合（不依赖 `pricing`/`config_store`）。验证：`test_usage_log.py` 新增「超过 200 条仍可翻页」「区间过滤含首尾」「每日聚合升序」「运行期滚动裁剪」4 个用例，并**更新现有 `test_rolls_back_file_when_over_threshold` 期望以匹配运行期滚动**（阈值 10 时第 11 条触发），全部全绿。
- [x] **任务 4：`admin_ui.py` API 扩展与配置白名单** — `/api/admin/usage` 解析并校验 `start`/`end`/`daily`（正则 + strptime 双重校验，非法或 `start > end` 返回 400），分页 `limit` 默认 100、上限 200，每次查询调用一次 `pricing.load_table()` 并用 `{**entry, ...}` 注入 `account`/`cost`/`credits`/`saved`，计算 `page_totals`，单条计价异常按未计价兜底；`admin_get_config()` 返回三个默认值；`EDITABLE_KEYS` 增加 3 个键；`admin_set_config()` 增加 `CODEARTS_PRICING` 专项校验（JSON、对象、有限非负、拒绝布尔、含 input/output）与 `CODEARTS_CREDITS_RATE` 浮点校验。验证：`test_admin_ui.py` 新增「区间过滤」「非法日期 400」「start>end 400」「daily 聚合」「账号注入」「单价表非法拒绝」「保存后立即生效」「脏数据不 500」8 个用例并全绿；同时更新现有 `TestUsage::test_usage_query` 对顶层 `total` / `limit` 的断言为 `page.total` / `page.limit`（响应契约已将这些字段移入 `page`）。
- [x] **任务 5：`UsageTab.tsx` 按截图重构** — 页头固定面包屑「主要 / 使用日志」、标题「使用日志」、副标题「按时间维度查看用量与请求明细」；时间预设（今日/7 天/14 天/30 天，均含当天，默认 7 天）+ 自定义区间 +「每日统计」切换 + 刷新、四张统计卡（副行文案按 design D8 固化）、8 列请求日志表（时间 `YYYY/MM/DD HH:mm:ss`、TOKEN 用量多行含输入 Tokens / 输出 Tokens / 缓存读取 / 缓存写入 / 输入总计加粗且输入总计 = 输入 + 缓存读取、费用 `$` + 4 位小数、KIRO CREDITS 4 位小数 +「积分」+ 省）、分页每页 100 条；每日统计以日聚合表替换明细表（10 列、不分页、空态、未计价「–」）；按模型分组改为可折叠面板（默认折叠），沿用现有 `by_model` 对象结构（模型 id → 聚合），不改为数组；本页 Token 含输入+输出+缓存读写，环形占比 = 缓存读取 ÷ 输入总计（输入 + 缓存读取）、一位小数、总量 0 显示 0%；用时取 `duration_ms`，无值显示「–」，首字耗时作悬浮提示；「省 x」以积分展示；切换区间重置页码；无单价显示「–」，token 千分位。验证：`npm run typecheck` 通过；启动 server 后在 `/admin` 目视核对 light/dark 两态。
- [x] **任务 6：`ConfigTab.tsx` 新增配置项** — 增加账号显示名、单价表（textarea）、Credits 换算系数三项，初值取后端默认值；单价表保存前用 `JSON.stringify(JSON.parse(text))` 压缩单行并做有限非负校验，错误时提示且不提交；账号名留空提交「主账号」。验证：`npm run typecheck` 通过；在配置页保存合法与非法单价表各一次，确认前者生效、后者报错。
- [x] **任务 7：文档同步** — `README.md` 配置表与 `config.example.json` 新增 `CODEARTS_ACCOUNT_NAME` / `CODEARTS_PRICING` / `CODEARTS_CREDITS_RATE` 三个键及说明（单价单位为 USD / 1M tokens，键为上游模型 id）；README 补充估算口径（cache token 为本地模拟、费用随单价表变动）；`CLAUDE.md` 架构章节补充 `pricing.py` 与三个配置键。验证：`config.example.json` 为合法 JSON；README 配置表条目与实际键名一致。
- [x] **任务 8：重建产物与全量测试** — `npm run build` 更新 `admin-ui/dist/`；运行 `pytest test_usage_log.py test_pricing.py test_admin_ui.py test_config_store.py test_anthropic_proxy.py test_cache_sim.py`。验证：dist 产物更新且 `git status` 仅含预期文件；全部测试通过。

## 验收标准

- [x] 使用日志页展示页头面包屑/标题/副标题与时间预设、自定义区间、每日统计切换、刷新控件
- [x] 四张统计卡分别展示总请求数（含分页副行）、本页费用（含均价）、本页 Credits（含节省额）、本页 Token（含缓存读取占比环形）
- [x] 请求日志表为 8 列：时间（`YYYY/MM/DD HH:mm:ss`）/ IP / 账号 / 模型 / TOKEN 用量 / 用时（无值「–」）/ 费用（`$` + 4 位小数）/ KIRO CREDITS（4 位小数 +「积分」）
- [x] TOKEN 用量含输入、输出、缓存读取、缓存写入，输入总计（= 输入 + 缓存读取）
- [x] 页头文案、统计卡副行与环形占比（一位小数）符合截图口径；请求日志每页 100 条
- [x] 未配置单价的模型费用与 Credits 显示「–」而非 0；费用与 Credits 固定 4 位小数
- [x] 日期区间过滤（严格 `YYYY-MM-DD`，非法或 start>end 返回 400）、每日统计切换、分页（默认 100、上限 200）均可用
- [x] 超过 200 条历史记录可完整翻页；运行期超 10000 条触发滚动且内存同步裁剪
- [x] 配置页可编辑账号名、单价表、Credits 系数；多行单价表可保存（前端压缩单行），非法单价表被拒绝且 config.json 不变
- [x] 记录端日志含 `cache_creation_tokens`，历史条目缺失时按 0 计价
- [x] 每日统计切换后以日聚合表替换明细表（10 列、不分页、空态与未计价显示正确），切回明细保留区间并重置页码
- [x] 按模型分组表以可折叠面板呈现，默认折叠
- [x] API 返回 `summary` / `by_model` / `entries` / `page` / `page_totals` / `daily` / `account` 完整字段；`daily` 关闭时为 `null`
- [x] 单价表含 `NaN`/`Infinity`/布尔值/负数时被拒绝；账号名留空回退「主账号」
- [x] 运行期滚动后 `summary` / `by_model` 同步重建；查询返回副本，派生字段不写回日志文件
- [x] 脏数据记录按未计价处理，查询不返回 500
- [x] 全部 pytest 用例通过，`npm run typecheck` 通过，dist 已重建，README 与 config.example.json 已同步
