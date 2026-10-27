# 任务清单：add-reasoning-tokens-usage-log

## 状态：ARCHIVED

## 任务

### 1. 存储层：reasoning_tokens 纳入聚合
- [x] `src/usage/usage_log.py` `_TOKEN_KEYS` 追加 `"reasoning_tokens"`（summary / by_model / daily 聚合自动覆盖）；同步模块级 `_summary` 初始化字面量与 `_by_model` 注释；模块 docstring 字段说明同步
- 验证：`python -m pytest tests/test_usage_log.py` 通过（24 passed）✅——新增 3 用例覆盖聚合累计 / 历史缺字段按 0 / `_summary` 字面量与 `_TOKEN_KEYS` 同步；既有 `test_daily_aggregation_ascending` 全字典断言同步补 `reasoning_tokens: 0` 期望值

### 2a. 采集层：共享提取函数与 OpenAI 端点接入
- [x] 新增共享提取函数（放 `src/usage/usage_log.py`）：`extract_reasoning_tokens(usage, reasoning_chars, output_tokens) -> int | None`，优先级 `usage.completion_tokens_details.reasoning_tokens`（int、非 bool 且 ≥0）→ `reasoning_chars // 4` → None；两来源统一截断 ≤ output_tokens
- [x] OpenAI 端点 `src/server.py`：`_record_usage` 签名加 `reasoning_chars` 参数并在条目写入 `reasoning_tokens`（非 None 时）；非流式调用点取聚合 result 的 `reasoning_content` 长度；流式 generate 循环累计 `delta.reasoning_content` 长度（含上游误回 JSON 分支）
- 测试落点：新建 `tests/test_server_usage.py`（mock 上游 + test_client，三路径断言）
- 验证：`python -m pytest tests/test_server_usage.py` 通过（8 passed）✅；`python -m pytest` 全量 530 passed ✅——覆盖上游细分优先 / 流式估算 / 非流式估算 / 截断 / 非法值退化 / 无来源省略键 / 误回 JSON 分支
- 备注：提取函数 9 项边界手验通过（细分优先、估算、估算截断、脏数据截断、负数退化估算、布尔视为无值、双来源皆无、脏 usage、output=0）；测试文件新增 `_NoopLease` + `_hmac_headers` stub，隔离 `test_oauth_login.py` 残留的 OAuth 未登录全局态（否则请求提前 503）；`_sse()` helper 用 `json.dumps` 构造合法 SSE 行（bytes 字面量内 `"x" * 400` 不会求值）

### 2b. 采集层：Anthropic 端点接入
- [x] Anthropic 端点 `src/proxy/anthropic_proxy.py`：`_record_usage` 同样接入提取函数；非流式取 result 的 `reasoning_content`；流式由 `AnthropicStreamConverter.feed()` reasoning 提取点（两分支共用处）累计字符数，暴露只读属性，`finish()` 后取值（不受 `thinking_as_text` 开关与限额丢弃影响）
- 验证：`python -m pytest tests/test_anthropic_proxy.py -k ReasoningTokens` 通过（6 passed）✅；全量 536 passed ✅——覆盖上游细分优先 / 估算兜底 / 省略键 / 脏 result 兜底 / 开关无关累计 / 端到端流式估算
- 备注：`_result_reasoning_chars` 在 `server.py` 与 `anthropic_proxy.py` 各留一份本地实现（后者禁止延迟 import server，经用户确认取舍）；converter 累计为纯计数（不持有全文），置于 `feed()` reasoning 提取点之后、两分支之前

### 3. admin API 与前端展示
- [x] `admin-ui/src/tabs/UsageTab.tsx`：`UsageEntry`/`GroupStat`/`DailyRow`/`Summary` 接口加 `reasoning_tokens`；明细行「缓存写入」与「输入总计」之间插「思考 Tokens」（缺失显示 –，文案与既有「思考强度」列区分）；按模型分组表与每日统计表追加「思考」列（缺失按 0）；每日表空态提示行 `colSpan` 由 `10` 同步为 `11`、明细表 `colSpan={8}` 保持不变（未新增列）；「本页 Token」卡片 note 追加思考小计；**`pageTokens` 汇总值计算保持四键不变**（reasoning_tokens 为 output 子集，加入即双重计入）
- [x] `npm run build` 重新产出 dist/ 并整体入库（唯一提交点）
- 验证：`tsc --noEmit` 零错误 ✅；build 成功产出 `dist/assets/index-DXbCtYkM.js`（70.57 kB）/ `index-BtICkyCQ.css`（54.73 kB）（列序对齐 spec 后重建，hash 由 `index-36XmN030` 变更），旧 hash bundle 同批删除 ✅；`tests/test_admin_ui.py` 80 passed ✅；全量 539 passed ✅

### 4. 端到端核验与文档
- [x] `python -m pytest` 全量通过
- [x] server 冒烟：mock 或真实请求一条含思考的流式 + 一条非流式，`usage-log.jsonl` 新条目含 `reasoning_tokens`；管理页明细行 / 分组表 / 每日表展示一致
- 验证：OpenAI 端点三条实测 —— 上游细分 250（stream，output 300）入库 250；纯 delta 估算 800 字符 → 200（output 500）入库 200；双来源皆无省略键 ✅。admin API 回读：summary.reasoning_tokens=450、by_model=450、daily=450，与明细 250+200+0 一致 ✅。Anthropic 端点两条实测 —— 流式 700 字符估算 175（output 400 未截断）、非流式上游细分 90 优先 ✅；客户端可见响应体 3367 字节，转发链路未改动 ✅
- [x] CLAUDE.md「使用日志与计价」小节补充思考 token 口径说明（注明：÷4 为英文近似，中文思考文本偏低估；估算值仅作量级观测）
- 验证：CLAUDE.md 追加一句（1 增 1 删），含提取优先级 / 估算口径 / 统一截断 / 省略键 / 不参与计价 / 中文偏低估 六点 ✅
- [ ] 归档时按 delta 合并两份 spec：`openspec/specs/usage-log/spec.md`（MODIFIED「使用统计查询 API」token 枚举含思考）+ `openspec/specs/admin-ui/spec.md`（MODIFIED「使用日志每日统计视图」列清单含思考、MODIFIED「使用日志思考强度列」语义区分声明）（阶段 4 归档步骤 3 执行，不计入阶段 3 完成口径）
- 验证：冒烟记录附在任务备注；文档 diff 一行说明；两份 baseline spec 归档后含「思考」枚举且结构完整

## 验收标准
- [ ] 双端点新日志条目含 `reasoning_tokens`（上游细分存在时为精确值，否则估算值，两者皆无时省略键）
- [ ] summary / by_model / daily 聚合含 reasoning_tokens 累计；历史记录按 0 计
- [ ] 使用日志页明细行展示「思考 Tokens」，缺失显示「–」；分组表与每日统计表有「思考」列（缺失按 0）
- [ ] 客户端可见响应体零变化（转发链路不修改 chunk / result）
- [ ] `python -m pytest` 全量通过；`tsc --noEmit` 零错误
