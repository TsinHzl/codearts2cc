# 技术设计：admin UI 对齐 codex2cc 样式

## 上下文

现有 admin-ui 为 React 18 + esbuild 单 bundle（App.jsx + 5 tabs，348 行），样式为手写六变量 CSS。codex2cc 前端为成熟设计体系：React 19 + TS + Vite 8 + Tailwind CSS 4 + lucide-react，jade-teal token、侧边栏折叠布局、glass-morphism、移动端 bottom nav。用户明确选择整栈迁移（方案 B），目标是视觉完全对齐。

## 目标 / 非目标

- 目标：admin-ui 视觉与布局完全对齐 codex2cc；数据面行为零变更
- 非目标：不引入 react-router / radix / recharts / i18n（单页五页签 state 切换已满足）；不改任何后端接口契约

## 决策

### D1：Vite 代替 esbuild，`base: '/admin/'`
沿用 codex2cc vite 配置蓝本：base 保证产物相对引用落在 `/admin/` 前缀下（与现有 Flask 302 重定向兼容）；manualChunks 仅保留 react-vendor / lucide-vendor 两项（无 recharts/radix/i18n 依赖，过度分包无收益）；`__APP_VERSION__` define 不需要（无版本展示需求，YAGNI）。

### D2：路由保持页签 state，不引入 react-router
五页签切换是纯展示层切换，codex2cc 的 BrowserRouter/basename 模式仅在多路由时必要。保持现有 tab state 语义，规避 URL 同步与 404 兜底等新问题。main.tsx 不需要 basename 判断。

### D3：Tailwind 4 token 层直接移植，dark 采用 class 策略
移植 codex2cc index.css 的 `@theme`、`@custom-variant dark`、字体栈、glass 变量与 keyframes。主题切换用 root class（`dark`），index.html 预挂载脚本在 React 加载前同步 localStorage，防 FOUC——与 codex2cc 一致。

### D4：`admin_ui.py` content-type 扩展
Vite 产物含 `assets/*.hash.js/css`、字体（woff2）、svg、map。`admin_asset` 的后缀判断需补 `.woff2`、`.svg`、`.map`（map 返回 JSON 类型）、`.json`；`<path:filename>` 本身已支持子目录，`_serve_admin_asset` 防穿越逻辑不变。

### D5：dist 产物继续提交入库
沿用现有约束：普通用户运行 server 零 Node 依赖。Vite 产物文件名带 hash，`Cache-Control: no-store` 已有，提交入库策略不变。

## 风险与权衡

| 风险 | 权衡 |
|---|---|
| React 19 + Vite 产物体积大于 esbuild 单 bundle | 本地 127.0.0.1 服务，无带宽压力；可接受 |
| Tailwind 4 构建要求较新 Node（≥20） | 仅影响开发者构建；用户侧不受影响 |
| 页签迁移中的行为回归 | 逐行对照迁移逻辑；test_admin_ui.py 补静态资源用例；启动冒烟五页签 |
| lucide-react 引入树摇体积 | manualChunks 单独分包，仅打包实际引用的图标 |

## 迁移方案

任务顺序按 tasks.md 1→6：工具链 → token 层 → Layout → 页签 → 服务端适配 → 集成验证。前端迁移期间旧实现保持可用，最后一并切换（dist 一次替换），无中间态双轨。

## 待决问题

无——影响 spec/方案/任务拆分的问题已在阶段 0 澄清（方案 B）。
