# 设计文档：refactor-admin-react

## 架构选型
- **React 18 + esbuild 单 bundle**：无 Node 基建的项目引入最小前端工程链。esbuild 以 devDependencies 固定 0.28.1（本机已验证），`npm run build` 产 `dist/app.js`；产物提交，运行时零 CDN / 零 Node。
- **不引入**：TypeScript（项目无前端类型诉求）、路由库（单页五页签用 useState 切换）、状态管理库（页签间无共享状态）、UI 组件库（沿用现有 CSS 变量主题）。

## 目录与构建
```
admin-ui/
  package.json      # react/react-dom 18.3.1 pinned; esbuild 0.28.1 dev
  index.html        # 壳：引 dist/app.js
  src/
    main.jsx        # createRoot(<App/>)
    api.js          # fetch 封装（401→登录态、429 退避文案、错误透传）
    styles.css      # 迁移原 CSS 变量主题（:root + dark）
    App.jsx         # 登录态探测 + 页签切换
    tabs/           # Overview / Models / Benefit(+CreditCard) / Usage / Config
  dist/             # 构建产物，提交入库
```

## 关键决策与权衡
1. **产物入库 vs 构建时生成**：选产物入库。运行 server 的用户零 Node 依赖，符合本项目「pip install 即用」的定位；代价是 bundle diff 噪音（一次性 ~140KB minified）。
2. **esc() 手工转义 → React 自动转义**：消除 XSS 手工维护面；设计层面禁止 `dangerouslySetInnerHTML`，CR 时作为硬检查项。
3. **后端零契约变化**：所有 `/api/admin/*` 请求/响应结构不变；仅 `/admin` 页面壳从内嵌字符串改为 `send_from_directory`。这是本变更唯一后端触点，风险集中在静态路径与 404 处理。
4. **积分卡片 spec 缺口**：归档变更 add-points-usage-card 交付的积分卡片在主 spec 无对应 Requirement，本次 delta 以「页签功能等价」Scenario 将其纳入 MODIFIED 的验收范围，不单独 ADDED（避免为已交付功能补写历史 spec）。
5. **前端无单测**：项目无 JS 测试基建，不为此引入 vitest 等（YAGNI）；验证靠 pytest 后端回归 + 手动验收清单（tasks 4.2）。

## 风险与缓解
- **静态服务路径穿越/404**：`send_from_directory` 天然防穿越；dist 外资源 404。
- **缓存**：bundle 文件名不带 hash，靠 Flask 默认 no-cache 行为 + 手动强刷；本项目单用户本地场景可接受。
- **测试适配**：`test_admin_ui.py` 若断言内嵌 HTML 内容，改为断言 index.html 服务与静态资源可达。
