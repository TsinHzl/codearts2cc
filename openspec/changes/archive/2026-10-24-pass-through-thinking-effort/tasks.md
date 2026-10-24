# 任务清单：pass-through-thinking-effort

## 状态：ARCHIVED

## 任务
- [x] 转换器透传 effort：`anthropic_messages_to_openai` 构建 `openai_body` 时，从 `body.thinking.effort` 提取（缺省补 `"high"`），写入 `reasoning_effort` 字段。验证：`tests/test_anthropic_proxy.py` 新增用例断言「自带 effort 透传原值」「缺省补 high」「非 dict thinking 补 high」
- [x] 补充单元测试：覆盖 effort 透传的三种分支（自带值 / 缺省 / thinking 非 dict），并回归既有 `test_image_rejected_without_support` 等用例确认转换器无副作用。验证：`python -m pytest tests/test_anthropic_proxy.py -q` 全绿

## 验收标准
- [x] `anthropic_messages_to_openai` 在请求自带 `thinking.effort` 时透传其原值到 `reasoning_effort`
- [x] 请求缺省 `thinking.effort` 时，`reasoning_effort` 写入默认值 `"high"`
- [x] `thinking` 非 dict 或缺省时同样补 `"high"`，不抛异常
- [x] 既有 Anthropic 转换器全部单测通过，无回归（97 passed，含新增 6 个 effort 用例）