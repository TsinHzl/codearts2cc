# 任务清单：add-usage-log-credits

## 状态：ARCHIVED

## 任务

### 1. 明细表新增积分列
- [x] `UsageTab.tsx` 的 `UsageEntry` 接口补 `credits?: number | null`
- [x] 明细表 `<thead>` 在「TOKEN 用量」后插入「积分」列
- [x] 明细表 `<tbody>` 对应位置渲染 `fmtCredits(e.credits)`；空态行 `colSpan` 改为 7
- 验证：`npm run typecheck` 零错误；`npm run build` 成功

### 2. 后端断言补充
- [x] `tests/test_admin_ui.py` 的 `test_usage_dirty_entry_not_500` 补 `entries[0]["credits"] is None` 断言
- 验证：`python3 -m pytest tests/test_admin_ui.py -q` 全通过

### 3. 产物与规格同步
- [x] `npm run build` 重新产出 `admin-ui/dist/` 并提交
- [x] 更新 `openspec/specs/admin-ui/spec.md`「使用日志页展示对齐」场景中的明细列清单
- 验证：dist 自洽（index.html 引用的资源全部存在）；主 spec 列清单与实现一致
- 备注：阶段 3 CR 处置——M1（新 bundle untracked，提交时显式 `git add`）在提交步骤执行；L2 delta spec 补首行标题并删除「← was」标注；L3 `.gitignore` 追加 `.coverage`；L4 历史项维持 known issue

## 验收标准

- 使用日志页「请求日志」明细表每行展示该条记录的积分，未计价模型显示「–」
- 列顺序为 时间 / IP / 账号 / 模型 / TOKEN 用量 / 积分 / 用时
- `python3 -m pytest tests/test_admin_ui.py -q` 全部通过
- 普通用户运行 server 零 Node 依赖，dist 已入库
