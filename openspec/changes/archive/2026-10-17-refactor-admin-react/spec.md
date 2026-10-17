# Delta Spec：refactor-admin-react

> 基准 capability：`admin-ui`（已存在，不写 Purpose）

## MODIFIED Requirements

### Requirement: 管理界面访问鉴权
现有鉴权语义（口令登录、指数退避、before_request 门禁）不变；页面壳由内嵌 HTML 字符串改为服务预构建静态文件（`/admin` 返回 index.html，静态资源同源服务），鉴权覆盖范围与行为保持一致。

#### Scenario: 访问管理页壳
- **WHEN** 未携带有效会话访问 `/admin`
- **THEN** 返回登录页壳（index.html + bundle），API 调用被 401 拒绝，行为与原内嵌 HTML 时代一致

### Requirement: 管理页面技术实现
管理页前端由 React 18 实现：源码 JSX 经 esbuild 预构建为单 bundle 提交入库（含 package-lock.json 保证可复现构建），运行 server 零 Node 依赖；五个页签（概览/模型/额度/使用日志/配置）的布局、数据来源与交互语义与原实现保持一致；静态资源由 before_request 门禁覆盖（未配置口令时 403），响应设 `no-store`；禁止 `dangerouslySetInnerHTML`。

#### Scenario: 页签功能等价
- **WHEN** 已登录用户切换五个页签
- **THEN** 各页签调用与原实现相同的 `/api/admin/*` 端点，展示相同数据结构与文案（含积分卡片三分支、secrets 掩码回写跳过行为）

#### Scenario: 重新构建产物
- **WHEN** 开发者在 `admin-ui/` 执行 `npm install && npm run build`
- **THEN** esbuild 以 devDependencies 固定版本产出 `dist/app.js`，提交后普通用户运行 server 无需 Node

### Requirement: 管理界面配置编辑
配置页 React 实现保持白名单键校验（EDITABLE_KEYS 单行可打印校验、SECRET_WRITABLE_KEYS 掩码/纯 `*` 回写跳过）的后端契约不变；前端提交行为与原实现一致，空白串提交时提示文案与后端跳过行为相符。

#### Scenario: secrets 回写跳过
- **WHEN** 用户保存配置且 secrets 字段保持掩码回显值或纯 `*` 串
- **THEN** 前端原样提交、后端跳过更新，实际存储值不变
