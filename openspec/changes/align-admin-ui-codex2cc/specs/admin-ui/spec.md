# admin-ui Specification Delta

## MODIFIED Requirements

### Requirement: 管理页面技术实现
管理页前端由 React（19）+ TypeScript 实现：源码 TSX 经 Vite 预构建（`base: '/admin/'`，产物提交入库含 package-lock.json），运行 server 零 Node 依赖；样式采用 Tailwind CSS 4 token 层（`@theme` jade-teal 色板 + `@custom-variant dark`），布局为 codex2cc 风格侧边栏体系（桌面折叠侧栏 + 移动端 topbar/bottom nav），视觉完全对齐 codex2cc 管理界面；五个页签（概览/模型/额度/使用日志/配置）的布局、数据来源与交互语义保持一致；静态资源由 before_request 门禁覆盖（未配置口令时 403），响应设 `no-store`；禁止 `dangerouslySetInnerHTML`。

#### Scenario: 页签功能等价
- **WHEN** 已登录用户切换五个页签
- **THEN** 各页签调用与原实现相同的 `/api/admin/*` 端点，展示相同数据结构与文案（含积分卡片三分支、secrets 掩码回写跳过行为）

#### Scenario: 重新构建产物
- **WHEN** 开发者在 `admin-ui/` 执行 `npm install && npm run build`
- **THEN** Vite 以 devDependencies 固定版本产出 dist 产物（含 hash 文件名与 assets/ 子目录），提交后普通用户运行 server 无需 Node

#### Scenario: 视觉对齐 codex2cc
- **WHEN** 用户访问管理界面（light 或 dark 模式、桌面或移动端视口）
- **THEN** 布局结构（侧边栏/移动端导航）与主题配色与 codex2cc admin 一致

#### Scenario: 静态资源类型覆盖
- **WHEN** 浏览器请求 Vite 产出的 `.js`/`.css`/`.woff2`/`.svg`/`.map` 等静态资源
- **THEN** 系统按正确 content-type 返回，未登录时被门禁拦截，响应携带 `no-store`
