# 变更提案：add-usage-log-page

## 背景

admin 管理界面目前只有概览/模型/额度/配置页，无法观测代理的实际调用情况：哪个模型被调用多少次、token 消耗量、请求来源与耗时均不可见。参考 kiro2cc 的使用日志页（统计卡 + 按模型分组表 + 请求明细表），为本代理新增等价能力。

## 目标范围

**在范围内：**
- 新增独立记录模块 `usage_log.py`：每次聊天补全请求（双端点）记录一条日志，JSONL 文件持久化（`usage-log.jsonl`，加入 .gitignore），重启后统计保留；支持按条数滚动清理
- 双端点埋点：`server.py`（OpenAI `/v1/chat/completions`）与 `anthropic_proxy.py`（`/v1/messages`）在响应完成后记录（含流式），标注来源端点；仅记录成功请求
- admin 新增「使用日志」tab：顶部统计卡（总请求数 / 输入 Tokens / 输出 Tokens / 缓存读取 Tokens）、按模型分组表（模型 / 请求数 / 输入 / 输出 / 缓存命中）、请求明细表（时间 / IP / 端点 / 模型 / 流式标签 / token 用量 / 首字与总耗时）
- 查询 API：`GET /api/admin/usage`（统计 + 明细分页）
- 无费用 / Credits 列（上游不返回计费信息，已与用户确认去掉）

**不在范围内：**
- 费用 / Credits 计费估算
- 日志导出（CSV 等）
- 失败请求 / 上游错误的记录与统计
- 多实例共享存储（单机单进程即可）

## 技术方案

- **记录器**：`usage_log.py` 提供 `record(entry)` 与 `query(...)`；写路径用独立锁串行 append JSONL（原子性靠单行写入），内存侧维护总量聚合（计数器 + 按 model 分组 dict），启动时从 JSONL 回放重建聚合
- **埋点方式**：依赖注入约束（禁止延迟 import server）→ `server.py configure()` 时把 `usage_log.record` 注入 `anthropic_proxy`；`server.py` 自身直接调用。记录点放在响应结束处（流式在最后一个 chunk 发完后，非流式在 JSON 返回前）
- **字段**：`ts / endpoint / ip / model / stream / effort(可空) / input_tokens / output_tokens / cache_read_tokens / first_token_ms / duration_ms`；明细表展示「输入总计 = 输入 + 缓存读取」
- **Token 来源**：上游 usage 字段（`prompt_tokens` / `completion_tokens`）。缓存读取 tokens 仅 Anthropic 端点经 cache 模拟层注入，OpenAI 端点恒为 0（口径如此，非缺陷）
- **成功定义**：仅上游正常完成（非流式聚合成功 / 流式正常 finish）才记录；上游业务错误（SSE error 帧）与客户端断开不记录
- **滚动**：文件超过阈值条数（如 10000）时启动时整文件重写保留较新一半（读旧 → 写 tmp → `os.replace` 原子替换，同 models-cache.json 模式）；启动回放同时重建全量聚合与内存最近 N 条明细，保证重启后统计与明细一致

## 预期影响

- 每请求新增一次文件 append（本地磁盘，微秒级）与内存 dict 更新；流式路径在收尾处执行，不增加首字延迟
- `usage-log.jsonl` 随使用增长，靠滚动清理控制体积
- admin 页面新增一个 tab，其余页面不受影响

## 风险

- **并发写交错** → 单条日志一次 `write` 调用 + 进程内锁，Flask 单进程部署下无跨进程竞争
- **启动回放大文件慢** → 滚动清理限制文件条数，回放量可控
- **上游 usage 缺失**（部分 benefit 流无 usage）→ 字段记 0 并容忍，统计口径以"有值之和"为准
- **磁盘写失败** → 记录失败仅打印警告，不影响代理主链路
