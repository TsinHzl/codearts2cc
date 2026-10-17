# 任务清单：refactor-admin-react

> 状态：IN_PROGRESS

## 1. 前端工程搭建
- [x] 1.1 创建 `admin-ui/package.json`（react/react-dom 18.3.1 固定版本；devDependencies: esbuild ^0.28.1；scripts: `build` = esbuild bundle --minify）与 `admin-ui/index.html` 壳（`<div id="root">` + 引 dist/app.js）
- [x] 1.2 `.gitignore` 追加 `admin-ui/node_modules/`
- [x] 1.3 `npm install && npm run build` 产出 `admin-ui/dist/`（含 `package-lock.json` 提交），验证 bundle 生成

## 2. React 组件实现（全部重写·保持布局）
> 每条任务的验收 = 对应「手动验收清单（4.2）」中的关联步骤，落地时逐项核对。

- [x] 2.1 共享层：`api()` fetch 封装（401 → 切登录页、429 → 退避文案、错误透传）+ `styles.css`（迁移现有 CSS 变量主题）。验收：验收清单步骤 A1-A3
- [x] 2.2 `App` + `LoginPage`。验收：清单 B1-B2（登录表单 / `POST /api/admin/login`）
- [x] 2.3 `OverviewTab`（GET /api/admin/overview）。验收：清单 C1
- [x] 2.4 `ModelsTab`（GET /api/admin/models + POST refresh + agent/benefit 分组与徽章）。验收：清单 C2
- [x] 2.5 `BenefitTab` 含 `CreditCard`（GET/POST benefit + GET credit 三分支文案）。验收：清单 C3（含 credit 三分支各命中一次）
- [x] 2.6 `UsageTab`（统计 + 分页明细，page/limit 交互与现状一致）。验收：清单 C4
- [x] 2.7 `ConfigTab`（白名单键编辑 PUT config + secrets 掩码/纯 `*` 回写跳过）。验收：清单 C5（空白串提交提示文案与后端跳过行为相符）

## 3. 后端接线（最小改动）
- [x] 3.1 `admin_ui.py`：移除 `_ADMIN_HTML`，`/admin` 改为 `send_from_directory` 服务 `admin-ui/dist/`，静态资源路由 + 404 处理 + 响应 `no-store`；before_request 门禁覆盖全部静态资源
- [x] 3.2 改写 `test_admin_ui.py:510-524` `test_admin_shell_contains_usage_tab` → 断言 200 + content-type + `id="root"`；新增「ADMIN_PASSWORD 未设置时 `/admin` 与静态资源均 403」测试

## 4. 验证
- [x] 4.1 `python3 -m pytest test_admin_ui.py` 通过；全量单测回归
- [ ] 4.2 手动验收清单（启动 server 逐项核对，完成后在 tasks 中标注各项通过）：
  - A1. 未登录调用 API → 401 → 前端切登录页
  - A2. 登录连错 5 次 → 429 退避文案展示
  - A3. API 网络错误 → 错误信息透传展示
  - B1. 登录表单提交 `POST /api/admin/login` 成功进入主界面
  - B2. 登出 → 回登录页
  - C1. 概览页签数据与后端返回一致
  - C2. 模型页签 agent/benefit 分组、徽章、强制刷新生效
  - C3. 额度页签：签到 + 积分卡片三分支（正常/未配置 400/会话失效 502）各命中一次
  - C4. 使用日志统计 + 分页明细翻页正常
  - C5. 配置页签：保存白名单键、secrets 掩码回写不覆盖、空白串提交提示
  - C6. 亮暗主题（prefers-color-scheme）渲染正常
- [ ] 4.3 README 补充前端构建说明（`admin-ui/` 下重新构建 bundle）

## 5. 收尾
- [x] 5.1 T7 代码审查（sub-agent，前台）：7 条发现（1 High + 6 Low），已修复 #1 刷新按钮失效、#3 index.html content-type、#4 掩码不载入输入框、#6 nosniff 头、#5 link 移入 head、#2 死导入删除；#7 dist 入库约定与 AuthError 死代码记入 cr-known-issues
- [ ] 5.2 产物提交（分支 `refactor/refactor-admin-react`，`refactor(refactor-admin-react):` 前缀）
