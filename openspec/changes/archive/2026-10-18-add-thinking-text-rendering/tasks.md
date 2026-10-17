# 任务清单：add-thinking-text-rendering

## 状态：ARCHIVED

## 任务
- [x] 1a. 新增 `format_thinking_as_text` / `format_duration_line`、ANSI 常量与 ESC 净化函数（CSI / OSC / 两字符转义，OSC 优先）；先写单测再实现；验证：`pytest test_anthropic_proxy.py -k "format_thinking or format_duration or esc"`
- [x] 1b. 新增 `strip_thinking_text_from_messages` / `is_claude_code_user_agent` 与正则常量；先写单测再实现；验证：`pytest test_anthropic_proxy.py -k "strip_thinking or user_agent"`
- [x] 2a. 扩展 `AnthropicStreamConverter`：`thinking_as_text` 参数、`thinking_text` 独立块类型、逐完整行 flush、空白段不产生块、content/tool_calls/finish_reason/finish() 四个收尾点；先写单测再实现；验证：`pytest test_anthropic_proxy.py -k "thinking_as_text and not limit"`
- [x] 2b. 扩展 `AnthropicStreamConverter`：单段/累计限额丢弃、多段思考独立计时与时长行（时长行尾随 `\n`）；先写单测再实现；验证：`pytest test_anthropic_proxy.py -k "thinking_as_text and limit"`
- [x] 3. 扩展 `openai_completion_to_anthropic` 支持非流式 thinking→text（复用 `format_thinking_as_text`，无时长行，不限额）；先写单测再实现；验证：`pytest test_anthropic_proxy.py -k non_stream_thinking`
- [x] 4. 在 `messages()` 路由接入历史剥离：UA 匹配时剥离 `body["messages"]`，再执行转换与缓存估算；先写单测再实现；验证：`pytest test_anthropic_proxy.py -k history_strip`
- [x] 5. 路由接线响应改写开关：覆盖 UA × `CODEARTS_THINKING_AS_TEXT` 2×2 组合，流式与非流式两条路径；验证：`pytest test_anthropic_proxy.py -k ua_gate`
- [x] 6. 新增配置键：`admin_ui.EDITABLE_KEYS`、`_EDITABLE_DEFAULTS["CODEARTS_THINKING_AS_TEXT"] = "1"`、`config.example.json`；补 GET 默认值回显与写入 `0` 后读取的往返测试；验证：`pytest test_admin_ui.py -k thinking_as_text`
- [x] 7. 管理页 `ConfigTab.tsx` 新增开关下拉并 `npm run build` 重新产出 dist；验证：`npm run typecheck` + 构建成功
- [x] 8. 更新 `README.md` 配置表与 Anthropic 端点说明、`CLAUDE.md` 对应段落，并移除 README 中写死的测试数量；验证：文档中提及 `CODEARTS_THINKING_AS_TEXT` 与文本化行为
- [x] 9. 全量回归：`python -m pytest test_anthropic_proxy.py test_cache_sim.py test_admin_ui.py test_config_store.py test_pricing.py test_usage_log.py` 全部通过（排除需启动服务的 `test_openai.py`）
- [x] 10. 真实客户端冒烟：以 `claude-cli` UA 抓包验证 SSE 渲染与时长行，并用真实 Claude Code 完成两轮对话验证历史剥离；验证：抓包记录与两轮对话输出

## 验收标准
- [x] Claude Code UA 请求时，流式响应不再出现 thinking 块，思考内容以 `💭 Thinking` 灰色文本逐行展示，每段结束显示 `💭 Thought for Xs` 并带尾随换行
- [x] 非流式响应中思考内容为独立 text 块（头行 + 灰色行，无时长行、不限额），正常回答保持独立 text 块
- [x] 思考后直接返回 tool_calls 时，thinking_text 块正确收尾，无孤立 content_block_stop
- [x] 非 Claude Code UA 或 `CODEARTS_THINKING_AS_TEXT=0` 时思考块为原生格式；CC UA 下历史剥离仍生效
- [x] 多轮对话历史中的思考文本被剥离，正常回答保留，缓存估算使用剥离后的消息
- [x] 单段 >256 KiB / 响应 >1 MiB 时停止文本化，无半行残留，无孤立 content_block_stop
- [x] 上游思考内容中的 ESC 控制序列被剥离
- [x] 管理后台配置页可切换开关，保存后立即生效
- [x] 上述 pytest 文件全量通过，`admin-ui` 构建成功
- [x] 真实 Claude Code 两轮对话验证通过
