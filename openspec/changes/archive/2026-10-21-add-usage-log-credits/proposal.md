# 变更提案：add-usage-log-credits

## 背景

`/api/admin/usage` 已为当前页每条明细返回 `credits`（费用 × `CODEARTS_CREDITS_RATE`）与 `cost` / `saved`，但前端 `UsageTab.tsx` 的「请求日志」明细表只展示时间 / IP / 账号 / 模型 / TOKEN 用量 / 用时，积分仅在「每日统计」聚合表可见。用户希望直接在每条日志上看到该次请求消耗的积分，无需切到聚合视图。

## 目标范围

**在范围内：**
- `admin-ui/src/tabs/UsageTab.tsx` 明细表新增「积分」列，位置在「TOKEN 用量」之后、「用时」之前，单元格复用现有 `fmtCredits`（4 位小数 + 「积分」后缀，`null` 显示「–」）
- `UsageEntry` 类型补 `credits?: number | null` 字段
- 空态行 `colSpan` 6 → 7
- `tests/test_admin_ui.py` 补一条断言：未配置单价时明细 `credits` 为 `null`（与既有 `cost is None` 断言并列）
- `npm run build` 重新产出 `admin-ui/dist/` 并入库
- `openspec/specs/admin-ui/spec.md` 中「使用日志页展示对齐」场景的列清单同步（经用户确认破例补 delta）

**不在范围内：**
- `/api/admin/usage` 后端契约（`credits` 字段已存在，行为零变更）
- 「按模型分组」表与「每日统计」表（前者无计价字段，后者已展示 Credits）
- 顶部统计卡（不新增本页积分合计）
- 明细表不展示 `cost` / `saved`

## 技术方案

纯前端展示层改动。明细表 `<thead>` 增加 `<th className={th}>积分</th>`，`<tbody>` 对应位置增加 `<td className={td}>{fmtCredits(e.credits)}</td>`；`fmtCredits` 已存在（每日统计表使用），无需新增工具函数。后端返回的 `credits` 由 `admin_ui._price_entries` 逐条注入，未计价模型为 `None`。

## 预期影响

- 影响文件：`admin-ui/src/tabs/UsageTab.tsx`、`admin-ui/dist/`（构建产物）、`tests/test_admin_ui.py`、`openspec/specs/admin-ui/spec.md`
- 净改动约 10 行源码 + dist 产物；无新增依赖、无 API 变更、无数据迁移
- 表格由 6 列变 7 列，窄屏下沿用既有 `overflow-x` 内滚，不改变布局体系

## 风险

- dist 产物为构建生成，需确认自洽（`index.html` 引用的 hash 资源全部存在）与 `pytest tests/test_admin_ui.py` 通过
- 明细列增多后移动端横向滚动量略微增加；现有 `data-slot="table-container"` 已提供内滚，风险低
