# 任务清单：add-usage-log-page

## 状态：ARCHIVED（2026-10-17 完成归档）

## 任务
- [x] T1 新增 `usage_log.py`：`record()` 加锁单行 append JSONL + 内存聚合；`query()` 返回统计与分页明细；启动回放重建聚合；超阈值滚动清理（验证：新增 `test_usage_log.py` 单测覆盖 record/query/回放/滚动）
- [x] T2 `server.py` 埋点：`chat_completions` 在流式收尾与非流式返回前调用 `record()`，提取 usage/IP/耗时/首字时间（验证：单测或手动冒烟，`usage-log.jsonl` 出现 endpoint=openai 记录）
- [x] T3 `anthropic_proxy.py` 埋点：`configure()` 注入 `record`；`messages()` 两路径收尾记录，endpoint=anthropic（验证：同上）
- [x] T4 新增 `GET /api/admin/usage`（admin_ui.py）：返回 `{summary, by_model, entries(分页)}`，分页参数 `page`(默认 1)/`limit`(默认 50，上限 200)，按时间倒序；走既有 admin 鉴权（验证：`test_admin_ui.py` 新增用例）
- [x] T5 admin 前端新增「使用日志」tab：统计卡 + 按模型分组表 + 明细表（时间/IP/端点/模型/流式+effort 标签/token 用量[输入、输出、缓存读取、输入总计]/首字与总耗时），无费用列（验证：`test_admin_ui.py` 校验 HTML 含 tab 与表格结构）
- [x] T6 `.gitignore` 加 `usage-log.jsonl`（验证：`git check-ignore` 通过）
- [x] T7 回归：`python -m pytest` 现有全部用例 + 新增用例全绿（验证：pytest 退出码 0）

## 验收标准
- [x] 双端点各发起一次流式 + 非流式请求后，`/api/admin/usage` 返回正确统计与 4 条明细（TestUsageRecording 双端点流式/非流式 + test_admin_ui TestUsage 覆盖）
- [x] 重启代理后统计与明细仍保留（JSONL 回放，test_usage_log.py 回放用例覆盖）
- [x] admin 页面「使用日志」tab 正常渲染统计卡、分组表、明细表（含输入总计列与 effort 标签，test_admin_ui.py TestUsageTab 覆盖）
- [x] 未登录访问 `/api/admin/usage` 返回 401（走既有 `_admin_gate`，test_admin_ui.py TestUsage.test_usage_unauthed_401 覆盖）
- [x] 上游 usage 缺失的请求记 0，统计不报错、不记 NaN（_record_usage 全 try/except 兜底 + `or 0` 收敛）
- [x] 日志文件超阈值时启动清理，清理后条数约为阈值一半（test_usage_log.py 滚动清理用例覆盖）
