# 任务清单：add-official-credit-pricing

## 状态：ARCHIVED

## 任务

### 1. 官方单价拉取（server.py）
- [x] 新增 `BUILTIN_MODELS_URL` 常量与 `_fetch_official_credit_pricing()` 辅助函数：`GET {BASE_URL}/v1/model/builtin`（`agent-type: PromptCenter`），只保留 `time_slot_id` 为 null 或 `default` 的档位（多档取首个），返回 `{model_id: {input_price, cache_price, output_price, time_slot_id}}`（原始字段），失败返回 `{}`
- [x] `_refresh_models()` 在 `discovered` 定稿前调用该函数，按 `model_id` 精确匹配（区分大小写），把 `credit_pricing` 合并进对应模型条目
- [x] `_save_model_cache` 落盘时保留 `credit_pricing` 字段
- 验证：启动 `python3 -m src.server` 后 `models-cache.json` 中 `deepseek-v4.1-flash` 条目含 `credit_pricing.input_price == "2"`；断网/未登录时仅打印警告且模型同步仍成功；带 `credit_pricing` 的模型 id 均能在 `builtinModels[].model_id` 中找到同名条目

### 2. 单价表归一化与合并（pricing.py）
- [x] 新增 `load_official_table(cache_path=None)`：读模型缓存，`credit_pricing` → `{input, output, cache_read, cache_creation}`（cache_read/cache_creation 取 `cache_price`），转换后复用 `_valid_number` 校验（拒绝负数 / `NaN` / `Infinity` / 布尔值），失败跳过该模型
- [x] 新增 `load_merged_table(cache_path=None)`：官方表为底、`CODEARTS_PRICING` 覆盖同名模型；返回 `(table, warning)`，warning 优先取手工表解析警告；透传 `cache_path`
- 验证：`python3 -m pytest tests/test_pricing.py -q` 通过

### 3. 管理面接入（admin_ui.py）
- [x] `admin_usage` 改调 `pricing.load_merged_table(model_cache_path)`（测试通过 `configure(cache_path=...)` 注入即时生效）
- 验证：未配置 `CODEARTS_PRICING` 时 `GET /api/admin/usage` 的 `entries[0].credits` 非 null

### 4. 测试
- [x] `tests/test_pricing.py` 补 7 例：官方表归一化、字符串价格解析、非法价格跳过、配置覆盖官方、无官方数据回退空表、多档位取 null/default 档、全部档位均非 null/default 时该模型未计价
- [x] `tests/test_admin_ui.py` 补 1 例：无手工单价表时明细 `credits` 由官方表计算且 `page_totals.priced_requests` 正确
- 验证：`python3 -m pytest tests/test_pricing.py tests/test_admin_ui.py -q` 全通过

### 5. 规格与文档同步
- [x] 核对 `openspec/changes/add-official-credit-pricing/specs/usage-log/spec.md` 的「费用与 Credits 计算」Requirement 与实现口径一致；基线 `openspec/specs/usage-log/spec.md` 的合并在归档阶段执行
- [x] `CLAUDE.md` 计价段落补一句官方表来源，并标注为本地估算口径
- [x] `README.md` 的「费用与 Credits 估算口径」（L163/L167）同步补充官方单价表来源与回退顺序
- 验证：spec 与实现口径一致；CLAUDE.md 描述可核对

## 验收标准

- 未配置 `CODEARTS_PRICING` 时，使用日志明细表与每日统计的积分列显示由官方单价表计算的具体数值（不再全为「–」）
- 已配置 `CODEARTS_PRICING` 时，同名模型以配置值优先，未覆盖的模型回退官方值
- 上游 `/v1/model/builtin` 拉取失败时模型同步不受影响，积分列回退「–」
- `python3 -m pytest tests/test_pricing.py tests/test_admin_ui.py -q` 全部通过
- 无新增运行时依赖，无新增配置文件
- 取一条已知 token 用量的真实请求，按 A′ 计算的积分与官方界面显示值核对，偏差需记录并说明原因
