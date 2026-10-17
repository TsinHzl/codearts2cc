# 变更提案：refactor-admin-react

## 背景
admin 管理页目前是 `admin_ui.py` 单文件内嵌的 HTML/CSS/JS 字符串（约 130 行原生 JS，单行内联函数长达上千字符），无组件化、无状态管理、不可单测前端逻辑、转义与 DOM 拼接手工维护。随积分卡片、secrets 回写等功能的加入，页面交互复杂度已超出「原生 JS 字符串模板」的合理规模。用户要求将 admin UI 改为 React 实现。

## 用户已确认的决策（阶段 0 澄清）
1. **引入方式**：本地文件 + 预构建 —— 前端源码（JSX）经 esbuild 预构建为单个 bundle.js 提交，Flask 服务静态文件；无运行时 CDN 依赖，离线可用（本机已验证 esbuild 0.28.1 可用）
2. **范围与布局**：五个页签（概览/模型/额度/使用日志/配置）全部重写，布局与现有 API 交互保持一致，仅技术栈替换
3. **旧前端处置**：`_ADMIN_HTML` 内嵌字符串完全移除，不保留双前端

## 目标范围
**在范围内：**
- 新增 `admin-ui/` 前端源码目录：React 18 源码（JSX）、独立 `package.json`（dependencies: react/react-dom 固定版本；devDependencies: esbuild）、esbuild 构建脚本
- 构建产物 `admin-ui/dist/`（单 bundle.js + index.html 壳）提交入库，Flask 直接服务静态文件——运行 server 无需 Node
- `admin_ui.py` 移除 `_ADMIN_HTML` 内嵌字符串，`/admin` 路由改为服务 `admin-ui/dist/index.html` + 静态资源
- 五个页签全部 React 组件化重写：概览 / 模型（含强制刷新）/ 额度（免费额度 + 签到 + 积分用量卡片）/ 使用日志（统计 + 分页明细）/ 配置（白名单键编辑 + secrets 回写）
- 视觉沿用现有 CSS 变量体系（`:root` 亮暗双主题、卡片/徽章/表格样式），样式抽为独立 CSS 随 bundle 引入
- `.gitignore` 补充 `admin-ui/node_modules/`
- README 补充前端构建说明（开发时如何重新构建 bundle）

**不在范围内：**
- 不改变任何 `/api/admin/*` 后端端点行为与契约（请求/响应 JSON 结构保持不变）
- 不做视觉改版 / 交互重组（保持现有页签布局与信息结构）
- 不引入路由库、UI 组件库、状态管理库（useState/useEffect 足够，页面间无共享状态）
- 不引入 TypeScript、不引入 Vite 完整工程链
- 不改动 admin 鉴权、退避限流、config_store 等后端逻辑

## 技术方案
- **构建链**：`admin-ui/package.json` + esbuild CLI（`esbuild src/main.jsx --bundle --minify --outfile=dist/app.js`），React 18（`createRoot`）；开发者在 `admin-ui/` 下 `npm install && npm run build` 重新生成产物；产物提交，普通用户零 Node 依赖
- **服务方式**：`admin_ui.py` 用 Flask `send_from_directory` 服务 `admin-ui/dist/`；`/admin` 返回 index.html，`/admin/assets/<file>` 返回静态资源；before_request 门禁覆盖 `/admin` 与全部静态资源（未配置口令均 403）；静态响应设 `no-store` 防旧 bundle 缓存
- **组件结构**：`App`（登录态探测 + 登录页/主应用切换）→ `Tabs` + 五个页签组件（`OverviewTab` / `ModelsTab` / `BenefitTab`（含 `CreditCard`）/ `UsageTab` / `ConfigTab`）；共享 `api()` fetch 封装与 `esc` 不再需要（React 自动转义，消除 XSS 手工转义负担）
- **API 契约**：与现有端点一一对应，不改后端；登录失败退避 429、积分失效 400/502 等错误文案原样透传展示

## 预期影响
- 后端 API 无任何变化，pytest 现有 admin 端点测试全部保持通过（仅页面壳路由的测试需适配：响应从内嵌 HTML 变为静态文件服务）
- `admin_ui.py` 缩减约 130 行前端字符串，职责收敛为纯后端 blueprint
- 前端逻辑首次可被 esbuild 编译期检查覆盖；运行时行为靠现有集成路径 + 手动验收
- 构建产物入库带来一次性的 diff 噪音（bundle 单文件，minify 后约 140KB，gzip 后更小）

## 风险
- **bundle 体积 / 提交噪音**：React 18 production bundle 约 140KB（minify 后约 45KB gzip 前体积），单文件一次性提交，后续仅业务代码变更时增量更新——可接受
- **构建环境差异**：esbuild 版本在 devDependencies 中固定（`0.28.1`，本机已验证可用），构建脚本写入 package.json scripts，避免版本漂移
- **XSS 面变化**：React 自动转义文本节点，替代手工 `esc()`，风险收敛而非扩大；但需注意 `dangerouslySetInnerHTML` 一律禁用（设计中明确禁止）
- **静态文件路径**：Flask 静态服务需正确处理 404 与缓存；index.html 壳无数据，鉴权语义与现状一致
- **测试适配**：`test_admin_ui.py` 第 510-524 行 `TestUsageTab::test_admin_shell_contains_usage_tab` 断言内嵌 HTML 内容（`data-tab="usage"` / `使用日志` 等），React 化后必然失效——改写为断言静态壳服务行为（200 + content-type + `id="root"` 挂载点），不保留对 bundle 内部文案的断言
- **未配置口令的安全回归**：`ADMIN_PASSWORD` 未设置时 `/admin` 返回 403，新增的静态资源路由必须同样被 before_request 门禁覆盖；验收清单补「未配置口令时 `/admin` 与静态资源均 403」断言
- **静态资源语义**：`send_from_directory` 防目录穿越；dist 外资源与不存在文件一律 404；bundle 不带 contenthash，Flask 对静态响应设 `no-store`（单用户本地场景，规避旧 bundle 缓存风险）
- **esbuild 版本**：0.28.1 为本机 `/Users/MacBook/My Files/Code/sourcetree/deepseek-harness/node_modules/.bin/esbuild` 实测版本；package.json 以 `^0.28.1` 固定，构建脚本以 `npm run build` 为准
- **构建可复现**：`package-lock.json` 一并提交入库，保证离线可复现构建
