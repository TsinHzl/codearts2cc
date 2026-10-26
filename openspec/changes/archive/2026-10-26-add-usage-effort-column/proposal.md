# 变更提案：add-usage-effort-column

## 背景
管理后台「使用日志」页的请求明细表当前展示时间 / IP / 账号 / 模型 / TOKEN 用量 / 积分 / 用时七列，
思考强度（effort）只作为模型列下方的灰色小字附带展示（`UsageTab.tsx:370`），无表头、无列语义，
既无法按列对齐阅读，也无法一眼看出「这条请求实际用了多深的推理」。

同时后端的 effort 采集链路口径不完整：
- Anthropic 端点从 `thinking.effort` 提取（`anthropic_proxy.py:129`），新版 Claude 协议的
  `output_config.effort` 完全未被读取；
- 记录逻辑不判断思考是否真的生效——`thinking.type=="disabled"` 的请求仍会把 effort 写进日志，
  而 `thinking` 缺省时又记为空值，两种语义混在同一条路径上；
- Anthropic 端点透传给上游的口径（`thinking.effort` → `reasoning_effort`，缺省补 `high`）
  与日志记录口径不一致，导致日志无法反映上游实际收到的推理强度。

参照仓库 `../kiro2cc-proxy` 的成熟实现（`resolve_recorded_effort`：thinking 生效时保留客户端值、
未携带补记 `high`，未生效一律丢弃），本次补齐「采集口径 + 列展示」两端。

## 目标范围
**在范围内：**
- `src/proxy/anthropic_proxy.py`：新增统一的 effort 解析函数，取值优先级
  `output_config.effort` → `thinking.effort`，两者均无则空；`thinking.type=="disabled"` 时返回 None
- 新增日志口径函数（对齐 `../kiro2cc-proxy` 的 `resolve_recorded_effort`）：
  思考未关闭时保留客户端显式 effort，未携带则记录 `high`；`thinking.type=="disabled"` 时记录 None
- 同文件的 `reasoning_effort` 透传统一改用该解析结果（缺省仍补 `high`），
  保证「日志记录的 effort」与「发给上游的 effort」同源
- 同文件的 `_record_usage` 改用该解析函数，替代当前只读 `thinking.effort` 的逻辑
- `src/server.py` 的 OpenAI 路径：`_record_usage` 内的 effort 归一化由当前的
  `isinstance(effort, str)` 收紧为「字符串且去空白后非空，否则 None」，
  使空串不再被记成 `""`，与 Anthropic 侧 `resolve_effort` 的口径一致
  （三处调用点的取值表达式 `req_json.get("reasoning_effort")` 本身不变）
- 管理后台「使用日志」明细表新增独立「思考强度」列，位置在「模型」列之后；
  有值展示 effort 文本，无值展示「–」；移除模型列下方原有的 effort 小字
- 补充 pytest 单测覆盖新解析函数与记录口径

**不在范围内：**
- 不改变上游 reasoning_effort 的默认值（仍为 `high`）
- 不新增按 effort 的聚合统计、筛选或排序能力
- 不回填历史日志条目（旧记录 effort 为 null 时展示「–」，与既有一致）
- 不引入新的前端依赖，不调整表格其他列的顺序与内容
- 不修改 `usage_log.py` 的存储与聚合逻辑（effort 本已是自由字段）

## 技术方案
1. **后端解析函数**（`anthropic_proxy.py`，纯函数、无副作用）：

   ```python
   def resolve_effort(body: dict) -> str | None:
       """按 output_config.effort → thinking.effort 顺序取客户端 effort；
       thinking 显式 disabled 时视为未开启思考，返回 None。
       空串与纯空白串视为未携带（与 OpenAI 路径归一化口径一致）。"""
       thinking = body.get("thinking")
       if isinstance(thinking, dict) and thinking.get("type") == "disabled":
           return None
       for source in (body.get("output_config"), thinking):
           if isinstance(source, dict):
               value = source.get("effort")
               if isinstance(value, str) and value.strip():
                   return value.strip()
       return None
   ```

2. **透传改造**：`anthropic_messages_to_openai` 中原有的「取 thinking.effort / 缺省补 high」替换为
   `openai_body["reasoning_effort"] = resolve_effort(body) or "high"`；
   由于 `resolve_effort` 对 `thinking.type=="disabled"` 返回 None，需保留原有「disabled 时直接返回
   不发该字段」的提前 return 分支，避免 disabled 请求被补成 `high`。
   **该提前 return 与 `resolve_effort` 内部的 disabled 判定是配套关系**：前者决定「不发字段」，
   后者决定「日志记 null」，两处均不可单独删除，实现时需在代码注释中注明。

3. **记录改造**：新增 `resolve_recorded_effort(body)`——`thinking.type=="disabled"` 时返回 None，
   否则返回 `resolve_effort(body) or "high"`（与 kiro2cc `resolve_recorded_effort` 同口径：
   思考生效时保留客户端值、未携带补记 `high`，未生效一律丢弃）；`_record_usage` 内 `effort`
   改由该函数产出。
   OpenAI 端点的 `_record_usage(model, ..., effort, ...)` 调用点取值表达式不变，
   函数内的归一化由 `isinstance(effort, str)` 改为「字符串且 `strip()` 后非空，否则 None」。

4. **前端**：
   - `UsageTab.tsx` 表头在「模型」与「TOKEN 用量」之间插入 `<th>思考强度</th>`；
   - 行内插入 `<td>{e.effort || '–'}</td>`（`e.effort` 类型已是 `string?`）；
   - 删除模型列内 `{e.effort && <span>{e.effort}</span>}`；
   - 空态 `colSpan` 由 7 调整为 8。
   - 改后执行 `npm run build` 重新产出 `admin-ui/dist/`（产物入库）。

## 预期影响
- 使用日志明细新增一列，表格从 7 列变 8 列；窄屏下横向滚动范围略增（容器已有 `overflow-x`）
- 新写入的日志条目 effort 取值更准确：`output_config.effort` 生效、disabled 请求记空、
  未携带 effort 的请求记 `high`（与发给上游的兜底值一致）
- 历史条目 effort 为 null，展示「–」，与现有空值列口径一致
- 上游 `reasoning_effort` 在客户端仅带 `output_config.effort` 时才开始生效（此前被丢弃为 `high`），
  属行为修正；未带任何 effort 字段时行为不变（仍 `high`）
- 无新增依赖，无数据库/文件格式变更，无迁移成本

## 风险
- **透传行为发生变化**：客户端只带 `output_config.effort` 时上游现在会收到该值（可能是 `low`），
  推理强度下降属预期语义，但需在验收时确认 Claude Code 等客户端不会被拖慢或变更输出质量预期。
- **disabled 判定依赖 `thinking.type` 字段**：若客户端以其他形式表达关闭思考（如仅带
  `budget_tokens: 0`），本次不识别，仍按未携带 effort 处理（补 `high`），与现状一致，不引入回归。
- **前端产物入库**：`admin-ui/dist/` 为仓库内提交产物，忘记重新构建会导致界面与源码不一致；
  任务清单中已单列构建与产物提交步骤。
- **既有测试断言**：`tests/test_admin_ui.py` 有构造 effort=None 的日志条目用例，属字段透传不受影响；
  新增列会改变前端 DOM，但该测试不解析表格列数，无需同步修订。
