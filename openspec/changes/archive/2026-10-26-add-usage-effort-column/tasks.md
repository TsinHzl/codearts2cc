# 任务清单：add-usage-effort-column

## 状态：ARCHIVED

## 任务

### 1. 新增 effort 统一解析函数
- [x] 在 `src/proxy/anthropic_proxy.py` 新增纯函数 `resolve_effort(body: dict) -> str | None`：按 `output_config.effort` → `thinking.effort` 顺序取首个 `strip()` 后非空的字符串（返回 `strip()` 后的值）；`thinking` 为 dict 且 `type=="disabled"` 时直接返回 `None`
- 验证：`python -m pytest tests/test_anthropic_proxy.py -q` 中新增用例通过（output_config 命中 / thinking 命中 / 两者皆有取 output_config / disabled 返回 None / 非 dict 返回 None / 空串与纯空白串返回 None / `output_config.effort` 为空串但 `thinking.effort` 有值时回退取后者）

### 2. 透传统一改用解析函数
- [x] `anthropic_messages_to_openai` 中 `reasoning_effort` 的取值改为 `resolve_effort(body) or "high"`，保留 `thinking.type=="disabled"` 时提前 return、不写该字段的既有分支
- 验证：既有 5 个 effort 透传用例（`test_effort_passthrough_with_value` / `test_effort_default_high_when_missing` / `test_effort_default_high_when_thinking_not_dict` / 空串 / 非字符串）全部通过；新增「仅 output_config.effort 时透传其值」用例通过

### 3. 日志记录改用解析函数
- [x] 在 `anthropic_proxy.py` 新增 `resolve_recorded_effort(body: dict) -> str | None`：`thinking.type=="disabled"` 时返回 `None`，否则返回 `resolve_effort(body) or "high"`（对齐 kiro2cc `resolve_recorded_effort`）
- [x] `_record_usage`（`anthropic_proxy.py`）中 `effort` 改为 `resolve_recorded_effort(body)` 产出，替换当前只读 `thinking.effort` 的实现
- [x] `src/server.py` 的 `_record_usage` 内 effort 归一化由 `isinstance(effort, str)` 收紧为「字符串且 `strip()` 后非空，否则 None」（三处调用点的取值表达式 `req_json.get("reasoning_effort")` 不变）
- 验证：新增用例断言「thinking enabled + effort=low → 记录 low」「output_config.effort=max → 记录 max」「未携带任何 effort 且未 disabled → 记录 high」「thinking disabled → 记录 None」「OpenAI 路径 reasoning_effort 为空串 → 记录 None」；`python -m pytest tests/test_anthropic_proxy.py tests/test_admin_ui.py -q` 通过

### 4. 前端表格新增思考强度列
- [x] `admin-ui/src/tabs/UsageTab.tsx` 表头在「模型」与「TOKEN 用量」之间插入 `<th>思考强度</th>`
- [x] 行内对应位置插入 `<td>{e.effort || '–'}</td>`，并在模型列内删除 `{e.effort && <span>{e.effort}</span>}`
- [x] 空态行的 `colSpan` 由 `7` 调整为 `8`
- 验证：`cd admin-ui && npx tsc --noEmit` 零错误

### 5. 构建产物与文档
- [x] `cd admin-ui && npm run build` 重新产出 `admin-ui/dist/` 并提交（仓库内入库产物）
- [x] `CLAUDE.md` 使用日志小节补充「思考强度列取 output_config.effort → thinking.effort」
- 验证：构建成功；`python -m pytest tests/test_admin_ui.py -q` 通过（静态资源与 HTML 断言不受影响）；启动 server 后 `/admin` 使用日志页可见新列且空值显示「–」

## 验收标准
- [ ] 使用日志明细表新增独立「思考强度」列，位于「模型」列之后，有值显示 effort 文本、无值显示「–」
- [ ] 模型列下方原有的 effort 小字已移除，effort 不在同一行出现两次
- [ ] 新写入日志的 `effort` 取值优先级为 `output_config.effort` → `thinking.effort`，均无则补 `high`
- [ ] `thinking.type=="disabled"` 的请求记录 `effort=null`，且发给上游的请求体不含 `reasoning_effort`
- [ ] 客户端仅携带 `output_config.effort` 时，该值同时出现在日志 `effort` 与上游 `reasoning_effort` 中
- [ ] 未携带任何 effort 字段时，上游 `reasoning_effort` 与日志 `effort` 均为 `high`
- [ ] OpenAI 路径的 `reasoning_effort` 为空串时，日志 `effort` 记为 null 而非 `""`
- [ ] 历史日志条目（`effort` 为 null）在新列展示「–」，不影响其他列
- [ ] `python -m pytest` 全量通过；`admin-ui` 的 `tsc --noEmit` 与 `npm run build` 通过
