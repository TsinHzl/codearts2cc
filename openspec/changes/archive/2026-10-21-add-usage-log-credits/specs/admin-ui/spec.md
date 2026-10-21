# admin-ui Specification Delta

## MODIFIED Requirements

### Requirement: 管理页面技术实现
管理页前端由 React（19）+ TypeScript 实现：源码 TSX 经 Vite 预构建（`base: '/admin/'`，产物提交入库含 package-lock.json），运行 server 零 Node 依赖；样式采用 Tailwind CSS 4 token 层（`@theme` jade-teal 色板 + `@custom-variant dark`），布局为 codex2cc 风格侧边栏体系（桌面折叠侧栏 + 移动端 topbar/bottom nav），视觉完全对齐 codex2cc 管理界面；概览/模型/额度/使用日志/API Key/配置各页签的布局、数据来源与交互语义保持一致，使用日志页的布局与交互以 `usage-log` capability 规格为准，API Key 页以 `api-key-auth` capability 规格为准；登录入口位于概览页（按钮 + 模态框承载 OAuth 登录流程，登录成功后按钮切换为退出登录），交互语义以 `oauth-login` capability 规格为准；静态资源由 before_request 门禁覆盖（未配置口令时仍放行 `/admin` 页面壳与静态资源，仅 `/api/admin/*` 返回 403），assets/ 带 contenthash 长 immutable 缓存、其余 no-store；禁止 `dangerouslySetInnerHTML`。

#### Scenario: 页签功能等价
- **WHEN** 已登录用户切换概览/模型/额度/使用日志/API Key/配置页签
- **THEN** 各页签调用与原实现相同的 `/api/admin/*` 端点，展示相同数据结构与文案（含积分卡片三分支、secrets 掩码回写跳过行为）

#### Scenario: 概览页登录入口
- **WHEN** 未登录或需重新登录时用户查看概览页
- **THEN** 「CodeArts 登录」行展示「登录」按钮，点击弹出模态框承载授权链接、短轮询、回调 URL 导入与取消登录
- **AND** 登录成功后模态框自动关闭并刷新概览，该按钮切换为「退出登录」
- **AND** 已登录且无需重新登录时该行展示「退出登录」按钮，点击后清除凭据并刷新概览

#### Scenario: 重新构建产物
- **WHEN** 开发者在 `admin-ui/` 执行 `npm install && npm run build`
- **THEN** Vite 以 devDependencies 固定版本产出 dist 产物（含 hash 文件名与 assets/ 子目录），提交后普通用户运行 server 无需 Node

#### Scenario: 视觉对齐 codex2cc
- **WHEN** 用户访问管理界面（light 或 dark 模式、桌面或移动端视口）
- **THEN** 布局结构（侧边栏/移动端导航）与主题配色与 codex2cc admin 一致

#### Scenario: 使用日志页展示对齐
- **WHEN** 用户打开使用日志页
- **THEN** 页头显示面包屑「主要 / 使用日志」、标题「使用日志」、副标题「按时间维度查看用量与请求明细」
- **AND** 时间列固定 `YYYY/MM/DD HH:mm:ss`；明细表列为时间 / IP / 账号 / 模型 / TOKEN 用量 / 积分 / 用时
- **AND** 明细「积分」列展示该条记录消耗的积分（费用 × `CODEARTS_CREDITS_RATE`，保留 4 位小数并带「积分」后缀）；模型未配置单价时显示「–」而非 0
- **AND** 费用（$）与节省额（$）仍仅保留在每日统计表，明细表不展示
- **AND** 明细「输入总计」= 输入 + 缓存读取；「用时」无值显示「–」
- **AND** 请求日志默认每页 100 条；顶部统计卡仅「总请求数」「本页 Token」两张；环形占比 = 缓存读取 ÷ 输入总计（输入 + 缓存读取），保留一位小数

#### Scenario: 静态资源类型覆盖
- **WHEN** 浏览器请求 Vite 产出的 `.js`/`.css`/`.woff2`/`.svg`/`.map` 等静态资源
- **THEN** 系统按正确 content-type 返回；未配置口令时静态资源仍放行（仅 `/api/admin/*` 返回 403）；已配置口令但未登录时被门禁拦截；assets/ 产物长 immutable 缓存、index.html no-store
