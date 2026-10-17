# design — add-usage-log-page

## 上下文
admin_ui.py 为 Blueprint 内嵌单文件 HTML；server.py 与 anthropic_proxy.py 之间存在依赖注入约束（禁止延迟 import server），server.py `configure()` 已注入 AGENT_MODELS / getter / 函数引用。无数据库，运行时文件已有原子写先例（models-cache.json、.admin_salt）。

## 目标 / 非目标
目标：双端点用量记录 + 持久化 + admin 展示。非目标：计费估算、失败请求记录、导出。

## 决策
1. **独立模块 `usage_log.py`**（非塞进 server.py）：可单测、双端点复用；通过参数注入到 anthropic_proxy，规避 import 约束。
2. **JSONL 追加 + 单行 write + 进程内锁**：单机单进程部署，无需 SQLite；崩溃最多丢最后一条未写完记录，读侧容忍坏行。
3. **启动回放重建聚合**：统计不依赖重新请求；回放同时执行滚动清理（超 10000 条截断最旧一半），控制文件与回放耗时。
4. **记录点在响应收尾**：流式在最后 chunk 后（与 proxy-finish 补块同区域），非流式在返回 JSON 前；拿不到 usage 记 0。
5. **字段不做消息内容记录**：隐私与文件体积考虑，只有元数据与用量。

## 风险 / 权衡
- JSONL 明细查询为全量内存过滤分页，1 万条内可接受；后续量大再引入 SQLite。
- 首字耗时在流式路径取第一个 content chunk 时刻，非流式无首字概念记 null。

## 迁移方案
无存量数据；首次启动自动创建空文件。

## 待决问题
无。
