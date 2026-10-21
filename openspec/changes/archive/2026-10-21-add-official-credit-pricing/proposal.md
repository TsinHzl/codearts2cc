# 变更提案：add-official-credit-pricing

## 背景

「使用日志」明细已支持展示每条记录的积分，但实测确认**上游聊天接口不返回积分**：`POST /api/v2/chat/completions` 的 `usage` 只有 `prompt_tokens` / `completion_tokens` / `total_tokens` / `reasoning_tokens`，流式与非流式一致，追加 `stream_options.include_usage` / `return_credits` / `with_credit` 等参数亦无变化。

官方客户端的「共消耗积分」来自另一接口 `GET {snap}/v1/model/builtin`（`Agent-Type: PromptCenter` + STS 签名），每个模型带 `credit` 单价数组：

```json
{"input_price": "2", "cache_price": "0.2", "output_price": "8",
 "ratio": "0.05", "ratio_display": "0.18x", "time_slot_id": "default"}
```

当前本仓库未配置 `CODEARTS_PRICING`，`pricing.load_table()` 返回空表 → `pricing.compute()` 返回 `None` → 明细积分列显示「–」。用户要求开箱即用显示积分。

## 目标范围

**在范围内：**
- `src/server.py` 在模型刷新（启动 + 300s TTL）时一并拉取 `/v1/model/builtin`，选定档位后把官方**原始**单价写入 `models-cache.json` 的 `models[].credit_pricing`（`{input_price, cache_price, output_price, time_slot_id}`，与模型列表同一次原子写）
- `src/usage/pricing.py` 新增 `load_official_table(cache_path=None)`：读取官方原始单价并**归一化**为与 `CODEARTS_PRICING` 同构的 `{model: {input, output, cache_read, cache_creation}}`；默认路径取 `paths.MODEL_CACHE_PATH`，测试可注入
- 口径 A′：`cost = (input×input_price + cache_read×cache_price + cache_creation×cache_price + output×output_price) ÷ 1e6`（USD，官方价即 USD / 1M tokens）；`credits = cost × CODEARTS_CREDITS_RATE`（默认 1.0），忽略 `ratio`
- 优先级：`CODEARTS_PRICING` 中显式配置的模型优先，缺失的模型回退官方表（合并后的表传给既有 `pricing.compute`）
- `src/admin/admin_ui.py` 的 `admin_usage` 使用合并后的单价表
- 多档位 `credit`（如 `openpangu-2.0-pro` 的 ≤32K / >32K、`deepseek-v4.1-flash` 的 default / off_peak）只取 `time_slot_id` 为 null 或 `default` 的档位（多档匹配取数组首个）；无匹配档位时该模型视为无官方单价，其余档位不参与计算
- 测试：`tests/test_pricing.py` 补官方表归一化与合并优先级用例；`tests/test_admin_ui.py` 补「无 `CODEARTS_PRICING` 时官方表生效」用例

**不在范围内：**
- 上游聊天接口的积分字段（不存在，不伪造）
- `CODEARTS_CREDITS_RATE`（仍只作用于 `credits = cost × rate`）
- 分时档位（off_peak）与按上下文长度分档的动态选择
- 积分余额接口（仍需 `CODEARTS_SNAP_SESSION`，本次不动）

## 技术方案

1. **拉取**：`_refresh_models()` 在拿到 `discovered` 后追加一次 `_signed_request("GET", BUILTIN_MODELS_URL, extra_headers={"agent-type": "PromptCenter", ...})`，解析 `builtinModels[].credit[]`，只保留 `time_slot_id` 为 null 或 `default` 的档位（多档取首个），按 `model_id` **精确匹配（区分大小写）**写入 `discovered[].credit_pricing` 的原始字段。拉取失败仅打印警告，不阻塞模型同步（单价缺失时积分显示「–」，与现状一致）。
2. **归一化**：`pricing.load_official_table(cache_path=None)` 读 `models-cache.json`，对每个 `credit_pricing` 非空的模型产出 `{"input": float(input_price), "output": float(output_price), "cache_read": float(cache_price), "cache_creation": float(cache_price)}`；转换后复用 `_valid_number` 校验（拒绝负数、`NaN`、`Infinity`、布尔值），失败时跳过该模型。
3. **合并**：`pricing.load_merged_table(cache_path=None)` = `{**official, **manual}`（manual 覆盖同名模型），`admin_usage` 传入 `model_cache_path` 调用（保证测试注入生效）。
4. **模型刷新缓存**：`credit_pricing` 随 `_save_model_cache` 一并落盘，不新增文件、不新增 TTL。

## 预期影响

- 影响文件：`src/server.py`、`src/usage/pricing.py`、`src/admin/admin_ui.py`、`tests/test_pricing.py`、`tests/test_admin_ui.py`、`openspec/changes/add-official-credit-pricing/specs/usage-log/spec.md`（delta）、`openspec/specs/usage-log/spec.md`（归档时合并）、`CLAUDE.md`
- 净改动约 120–160 行；新增 1 个上游 GET（仅在模型刷新时，频率与现有模型同步一致）
- 未配置 `CODEARTS_PRICING` 的用户从「全部显示 –」变为「显示官方积分」；已配置的用户中，显式配置的模型计价不变，配置未覆盖的模型由「–」变为按官方积分显示（升级行为变化）
- `cost` / `credits` 为本地估算口径（官方价视为 USD / 1M tokens 记账）；未配置 `CODEARTS_CREDITS_RATE` 时 `credits = cost`，界面与文档均标注为估算

## 风险

- 官方 `credit` 的 `ratio` 字段语义未确认，本次明确忽略；若官方实际以 `price × ratio` 计费，积分会偏高约 20 倍，需后续实测校准（已记录为已知取舍）
- 忽略 `ratio` 与只取首档决定了计算结果可能与官方界面显示值存在倍数级偏差，本次书面接受该取舍；实施后须用一条真实请求核对并记录偏差
- 分档模型只取首档，长上下文或 off_peak 场景金额不精确
- 官方价的货币单位无法从接口确认，按 USD / 1M tokens 记账；若实际为「积分 / 1M tokens」，每日统计费用列的 `$` 标签会误导，`credits` 数值不受影响
- `models-cache.json` 结构新增字段，旧缓存无该字段时归一化为空表（回退现状行为，无破坏）
- 拉取 `/v1/model/builtin` 需要有效 STS 凭据；未登录时静默失败，不影响模型同步
