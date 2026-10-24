# 变更提案：fix-upstream-concurrency-502

## 背景
华为云 CodeArts 上游聊天接口存在严格的账号级并发上限（最多 3 个并发请求）。当 Claude Code 等客户端同时发起主对话、后台标题/摘要生成、工具调用或子智能体请求时，代理层缺乏本地并发门控、流式/非流式连接未在所有退出路径闭环释放、重试循环复用过期签名头，且上游以 HTTP 400/403 或 HTTP 200 错误体返回的并发超限响应绕过了重试逻辑，导致频繁向客户端返回 `502`（最多三个请求）。

## 目标范围
**在范围内：**
- **本地并发信号量门控**：双端点（`/v1/chat/completions` 与 `/v1/messages`）共享本地并发门控，默认上限 `3`（支持 `CODEARTS_MAX_CONCURRENCY` 配置，排队超时支持 `CODEARTS_QUEUE_TIMEOUT` 默认 `120` 秒），第 4 个���以上并发请求在本地排队等待槽位，避免直接冲击上游触发限流。
- **连接与并发槽位生命周期闭环**：使用幂等租约（Lease）绑定并发槽位与 `upstream` 响应对象；非流式响应解析完毕后在 `finally` 中立即关闭连接并释放槽位；流式响应在生成器 `finally` 与 `response.call_on_close` 双重兜底下确保正常结束、异常中断、客户端断连（`GeneratorExit` / `BrokenPipeError`）或未迭代响应时均立即 `upstream.close()` 并释放槽位，消除僵尸连接占坑。
- **并发超限错误体识别与重试**：扩充 `_post_chat_with_retry` 判定条件，将携带并发/限流特征文本（如“最多”“并发”“too many”“rate limit”“throttl”“concurrent”等）的 HTTP 400/403 响应及 HTTP 200 非流式/JSON 业务错误响应纳入本地退避重试。
- **重试按次刷新签名头**：`_post_chat_with_retry` 支持传入签名构造函数（`headers_fn`），每次重试重新生成含最新 `x-sdk-date` 的 `SDK-HMAC-SHA256` 签名头，同时向后兼容直接传入 `dict`。

**不在范围内（Non-goals）：**
- 不修改现有的 `reasoning_effort` 默认 `"high"` 注入与透传逻辑。
- 不修改管理后台前端 UI 页面。
- 不引入 Redis 等外部进程间分布式锁（仅针对单进程多线程代理实例做进程内并发控制）。

## 技术方案
- 在 `src/server.py` 实现基于 `threading.Condition` 的可动态读取 `CODEARTS_MAX_CONCURRENCY`（默认 `3`）的幂等并发租约 `_UpstreamLease`，通过 `anthropic_proxy.configure()` 注入至 `src/proxy/anthropic_proxy.py`，保证 OpenAI 与 Anthropic 双端点共享同一并发池。
- `_UpstreamLease` 提供 `attach(upstream)` 与幂等 `release()` 方法，`release()` 内部先安全调用 `upstream.close()` 再归还并发计数并唤醒等待线程，重复调用只生效一次。
- 升级双端点的 `_post_chat_with_retry(body, headers_or_fn)`：每次尝试动态调用签名函数生成新签名头；新增 `_is_retryable_upstream(upstream)` 辅助判定，除 `429/500/502/503/504` 外，对 `400/403` 及 `Content-Type` 非 `text/event-stream` 的 `200` JSON 错误体检测并发限流关键字，命中则关闭连接并退避重试。

## 预期影响
- 彻底消除多并发请求直接打穿 CodeArts 3 并发上限引发的 502 报错与客户端断流重发导致的僵尸连接占槽问题。
- 保持对外 OpenAI / Anthropic API 契约与现有测试兼容。

## 风险
- **流式生成器未被消费导致槽位泄漏风险**：通过 `response.call_on_close(lease.release)` 与生成器 `try...finally: lease.release()` 双重绑定，确保即使 WSGI 层在迭代前断开也能释放槽位与上游连接。
- **本地排队过久超时风险**：设置排队等待上限（默认 120s），超时后立即释放并返回清晰的 503/502 繁忙错误，防止线程无限挂起。
