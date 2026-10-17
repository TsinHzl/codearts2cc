# 变更提案：admin UI 对齐 codex2cc 样式（align-admin-ui-codex2cc）

## 背景

当前 `admin-ui/` 为 React 18 + esbuild 单 bundle 实现（App.jsx + 5 个 tabs，共约 348 行），样式为手写 `styles.css`（六变量 + 旧类名）。用户要求「将当前仓库的 admin 的 UI 样式完全改成 codex2cc 的 admin 的 UI 样式」。参照仓库位于本地 `../codex2cc/`（前端源码在其 `frontend/` 子目录，token 层 `frontend/src/index.css`、布局蓝本 `frontend/src/components/Layout.tsx`、构建配置 `frontend/vite.config.js`）。codex2cc 前端为 React 19 + TypeScript + Vite 8 + Tailwind CSS 4 + lucide-react，具有 jade-teal 主题 token、侧边栏折叠布局、glass-morphism、移动端 bottom nav 等完整设计体系。经用户决策（AskUserQuestion），选择**方案 B：整栈迁移 codex2cc 技术栈**——admin-ui 迁移到 Vite + Tailwind CSS 4 + TypeScript + lucide-react，直接复用 codex2cc 的 index.css token 层与组件写法，还原度最高。

## 目标范围

### 在范围内

- `admin-ui/` 前端整栈迁移：esbuild → Vite 8（`base: '/admin/'`、manualChunks 分包）、JSX → TSX（strict tsconfig）、styles.css → codex2cc index.css token 层移植（`@import "tailwindcss"` + `@theme` + `@custom-variant dark`）
- 布局重写为 codex2cc Layout 体系：CSS Grid 侧边栏（`264px minmax(0,1fr)`，折叠 64px）、折叠动画（Apple HIG 缓动）、glass-morphism 变量注入、主题预挂载脚本（防 FOUC）、移动端 topbar + bottom nav
- 引入 lucide-react 图标；五个页签（概览/模型/额度/使用日志/配置）全部改写为 TS + Tailwind 组件，视觉完全对齐 codex2cc 风格
- 登录页按 codex2cc 卡片风格重写
- `admin_ui.py` 静态资源服务适配：Vite 产物带 hash 文件名且含 `.woff2/.svg/.map` 等类型，content-type 判断需扩展
- 五页签数据面保持不变：调用相同的 `/api/admin/*` 端点，数据结构与文案语义（含积分卡片三分支、secrets 掩码回写跳过）不变
- dist 产物提交入库，运行 server 零 Node 依赖（沿用现有约束）

### 不在范围内

- `/api/admin/*` 后端接口契约（鉴权、退避、白名单校验、.env 写回）——行为零变更
- codex2cc 的 recharts 图表、radix-ui 组件、i18n 国际化（本仓库单语中文，页签文案保持中文硬编码）
- server.py / anthropic_proxy.py / benefit.py 等非 admin 模块

## 技术方案

1. **脚手架**：package.json 换为 react 19 + typescript + vite + @tailwindcss/vite + lucide-react；vite.config.ts 仿 codex2cc（`base: '/admin/'`、`@` alias、manualChunks）；tsconfig 仿 codex2cc strict + Bundler resolution；index.html 加主题预挂载脚本。构建命令 `npm run build` → `tsc && vite build`，产物仍提交 `admin-ui/dist/`。
2. **样式层**：移植 codex2cc `index.css` 的 token 层（jade-teal `@theme` 色板、dark variant、字体栈 IBM Plex Sans/Noto Sans SC + IBM Plex Mono、glass 变量、动画 keyframes），删除旧 styles.css。
3. **布局**：新建 `Layout.tsx` 仿 codex2cc Layout（按本仓库页签裁剪 navDefs：概览/模型/额度/使用日志/配置），保留侧栏折叠 localStorage 持久化与移动端适配；路由仍用页签 state（单页无多路由，不引入 react-router）。
4. **页签**：5 个 tabs 改写为 `.tsx`，类名全部换 Tailwind utility + 语义 token（`bg-card`、`text-muted-foreground` 等），逻辑与 API 调用保持原语义。
5. **服务端**：`admin_ui.py` `admin_asset` content-type 扩展（.woff2/.svg/.map/.json 等），dist 子目录（`assets/`）支持；`_serve_admin_asset` 防穿越逻辑保留。

## 预期影响

- 影响文件：`admin-ui/`（全量重写：package.json、vite.config、tsconfig、index.html、src/ 全部）、`admin_ui.py`（content-type 微调）、`openspec/specs/admin-ui/spec.md`（MODIFIED「管理页面技术实现」）
- 预计净改动 > 800 行（前端重写为主）；构建后 dist 产物体积可能增大（Tailwind + lucide），以迁移前后 dist 总字节数对比为度量记录，本地 127.0.0.1 no-store 服务无带宽压力，不设硬性上限
- 新运行时依赖边界：仅 react 19 + lucide-react；不引入 react-router / radix / recharts / i18n / ESLint / 前端测试框架（沿用 Python 侧 pytest + 集成冒烟作为质量门）
- 风险：Vite 产物路径带 hash，需确认 Flask `<path:filename>` 与 `assets/` 子目录服务正确；React 19 + Tailwind 4 产物面向现代常青浏览器（esbuild 目标 esnext，与 codex2cc 一致，不提供旧浏览器降级——管理界面为本地单账号自用，接受该取舍）；现有 no-store/防穿越逻辑保留，`.map` 文件随产物提交入库（本地 127.0.0.1 单账号场景无源码暴露面，与现有 bundle 无 map 的差异可接受）

## 风险

| 风险 | 缓解 |
|---|---|
| Vite hash 产物提交入库导致 dist 目录膨胀 | build 后仅提交 dist 必要文件；no-store 头已有 |
| Tailwind 4 需 Node 较新版本构建 | 仅开发者构建时需要 Node，普通用户零 Node 不变 |
| 页签行为回归 | 保持 API 调用与状态逻辑逐行对照迁移，集成冒烟验证五页签 |
