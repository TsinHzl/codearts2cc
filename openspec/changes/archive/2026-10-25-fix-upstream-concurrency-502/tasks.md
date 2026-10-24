# 任务清单：fix-upstream-concurrency-502

## 状态：DONE

## 任务
- [x] 任务 1：在 `src/server.py` 与 `src/proxy/anthropic_proxy.py` 实现共享的幂等并发租约门控（默认最大并发 3，支持 `CODEARTS_MAX_CONCURRENCY` 与 `CODEARTS_QUEUE_TIMEOUT` 配置），并通过 `configure()` 注入双端点共用（通过并发阻塞与释放单元测试验证）
- [x] 任务 2：重构 `src/server.py` 与 `src/proxy/anthropic_proxy.py` 的 `_post_chat_with_retry`，支持按次调用签名函数刷新 `x-sdk-date` 签名头，并识别 HTTP 400/403 及 HTTP 200 JSON 错误体中的并发/限流报错进行退避重试（通过单元测试验证签名按次刷新与并发超限错误体自动重试）
- [x] 任务 3：补齐 `/v1/chat/completions` 与 `/v1/messages` 流式/非流式路径的连接与并发槽位生命周期闭环释放（非流式 `finally` 释放，流式生成器 `finally` + `response.call_on_close` 幂等关闭 `upstream` 并释放槽位，通过断连与异常流单元测试验证）
- [x] 任务 4：在 `tests/` 补充并发信号量限制、流式断连释放槽位、重试签名刷新及并发超限错误体退避重试的回归测试，并运行 `python -m pytest` 全量通过（通过 `pytest` 0 失败验证）

## 验收标准
- [x] 同时发起超过 3 个上游请求时，代理在本地排队控制并发不超过 `CODEARTS_MAX_CONCURRENCY`（默认 3），不直接打穿上游并发限制
- [x] 流式与非流式请求在正常完成、上游报错、客户端中途断连（`GeneratorExit` / `BrokenPipeError`）或响应关闭时，均幂等执行 `upstream.close()` 并归还并发槽位，无槽位或连接泄漏
- [x] `_post_chat_with_retry` 在每次重试时重新生成签名头（刷新 `x-sdk-date`），并对包含并发/限流语义的 400/403 及 200 JSON 业务错误执行退避重试
- [x] 保持现有 `reasoning_effort` 默认 `"high"` 逻辑不变，且 `python -m pytest` 全量测试通过
