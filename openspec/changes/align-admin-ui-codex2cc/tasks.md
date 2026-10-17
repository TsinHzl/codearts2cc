# 任务清单：admin UI 对齐 codex2cc 样式

## 状态：IN_PROGRESS

## 任务

### 1. 脚手架迁移（Vite + TS 工具链）
- [x] 重写 `admin-ui/package.json`（react 19、typescript、vite、@tailwindcss/vite、lucide-react；build = `tsc && vite build`；依赖版本与 `../codex2cc/frontend/package.json` 对齐 pin）
- [x] 新建 `admin-ui/vite.config.ts`（base `/admin/`、`@` alias、manualChunks 分包）
- [x] 新建 `admin-ui/tsconfig.json`（strict、moduleResolution Bundler、`@/*` paths）
- [x] 改写 `admin-ui/index.html`（主题预挂载脚本、/admin/ 相对引用）
- 验证：`npm install && npm run build` 成功产出 dist/；依赖版本与 codex2cc 对齐

### 2. 样式 token 层移植
- [x] 以 codex2cc `index.css` token 层为蓝本新建 `admin-ui/src/index.css`（@theme jade-teal 色板、@custom-variant dark、字体栈、glass 变量、keyframes）
- [x] 删除 `admin-ui/src/styles.css`
- 验证：构建无 CSS 报错；`@theme` 色板变量名与 `../codex2cc/frontend/src/index.css` 逐一 diff 无差异（light/dark 两套齐全）✅（唯一差异为已剔除的业务专属变量 --account-state-* 4 项，属 codex2cc 账号状态遮罩业务样式，本仓库无对应场景）
- 备注：任务 1 CR 修复——tailwindcss 移入 devDependencies；index.html 入口临时指 `/src/main.jsx`、build 脚本临时 `vite build`（任务 4/5 迁移 TSX 后恢复 `/src/main.tsx` + `tsc && vite build`）

### 3. Layout 布局重写
- [x] 新建 `admin-ui/src/components/Layout.tsx`（Grid 侧边栏、折叠动画、glass 变量注入、移动端 topbar/bottom nav）
- [x] navDefs 按五页签裁剪（概览/模型/额度/使用日志/配置），lucide 图标
- 验证：`npm run build` 成功（114ms，22 modules）；按钮式导航（无 react-router 依赖，本仓库为单页签状态机非路由）；glass 注入 effect 保留蓝本骨架（backgroundImage 恒 null 走清理分支）；新增 `src/lib/utils.ts`（cn = clsx + tailwind-merge，与蓝本一致）
- 备注：Layout.tsx 本任务不接线 App.jsx（任务 5 迁移页签时统一接线）；useTheme hook 独立实现（localStorage 'theme' 键与 index.html 预挂载脚本一致）

### 4. 登录页与入口迁移
- [x] `api.js` → `api.ts`（类型化，五页签共用）
- [x] 登录页卡片重写（Tailwind 类）
- [x] `main.tsx` 入口（React 19 createRoot）
- 验证：`tsc --noEmit` 零错误 ✅；`npm run build` 成功（208ms，lucide-vendor 分包生效）
- 备注：新建 api.ts（`api<T>` 泛型 + ApiOptions，401→AuthError 行为等价）/ main.tsx（StrictMode）/ App.tsx（登录卡片 Tailwind 化 + 接线 Layout，引入 loggingIn 防重复提交态）；删除 api.js / main.jsx / App.jsx；index.html 入口恢复 `/src/main.tsx`；新增 src/vite-env.d.ts（vite/client 引用，解决 `import './index.css'` TS2307）；五个 tabs/*.jsx 的 `from '../api.js'` 统一切到 `from '../api'`；App.tsx 对五个 .jsx import 挂 `@ts-expect-error` 过渡注记（任务 5 逐个迁移时随 import 改写删除）；build 脚本仍为 `vite build`（任务 5 全部迁完 src 无 .jsx 后恢复 `tsc && vite build`）
- 备注：任务 3 CR 修复——Medium-2 More sheet 焦点管理（moreTriggerRef/moreCloseRef + aria-controls + 打开移焦/关闭还焦）、Medium-3 折叠态导航 label span 补 aria-hidden={sidebarCollapsed}；顺带修复 tsc 暴露的 Layout.tsx 未使用 import（useCallback/useMemo）、glass 死分支 TS2367/TS2345（glassOpacity/panelBlur 显式 number 注解、borderOpacity 0→'transparent'）
- 备注：任务 4 CR 处置——H1（旧页签依赖类随 styles.css 删除而缺失，过渡期裸渲染）经用户确认不补兼容层，任务 5 迁 TSX 时自然消除；M1 修复（moreTriggerRef 拆为 topbarMoreRef/bottomMoreRef + lastTriggerRef 还焦来源）；M2 修复（sheet 项 onClick 先 setMobileMoreOpen(false) 不再依赖 active 变化）；M3 修复（顶栏 Menu 补 aria-haspopup/aria-expanded）；M4（焦点陷阱）/M5（pytest 回归，任务 6/7 范围）/L1-L6 记录为 known issue；animate-in/slide-in-from-bottom-4/fade-in-0 三类为 tw-animate-css 插件类未安装实际空操作（sheet 无入场动画），已声明接受

### 5. 五页签逐个迁移 TSX（每页签迁移后单独核对 API 调用与文案语义）
- [x] OverviewTab → TSX（API：/api/admin/overview；字段逐一对照原 JSX）
- [x] ModelsTab → TSX（API：/api/admin/models + /models/refresh；双通道分组与刷新交互不变）
- [x] BenefitTab → TSX（API：/api/admin/benefit + /benefit/claim；积分卡片三分支语义不变）
- [x] UsageTab → TSX（API：/api/admin/usage；分页参数与时间倒序展示不变）
- [x] ConfigTab → TSX（API：/api/admin/config + /config/secrets；secrets 掩码回写跳过行为不变）
- 验证：`tsc --noEmit` 零错误 ✅；`npm run build`（tsc && vite build）成功（234ms，1931 modules，CSS 51.30 kB）✅；每个页签的 API 调用、状态逻辑与原 JSX 逐行对照一致 ✅（迁移中修复 data.page 可能为 undefined 的 TS18048，语义等价兜底 ?? 1）
- 备注：样式层全部按 codex2cc token 重构（data-slot="card"/status-tile/table-head/table-row、stat-card-accent、data-table-shell、usage-logs-table）；旧 .card/.muted/.badge 等 classes 全部消除（任务 4 CR H1 随之关闭）；ModelsTab 新增 refreshing loading 态、BenefitTab claim 补 claiming guard（语义增强，行为不变）；App.tsx 去掉全部 @ts-expect-error 过渡注记；package.json build 恢复 `tsc && vite build`
- 备注：任务 5 CR 处置——M1 修复（index.css @theme 补 --color-info light hsl(198 62% 38%) / dark hsl(198 52% 55%)，端点/流式徽章恢复蓝色语义）；M2 修复（UsageTab 明细表 td 补 data-slot="table-cell" + 外层补 div[data-slot="table-container"] 激活内滚+sticky；ModelsTab 数值列 td 补 table-cell；ConfigTab 三个控件补 data-slot="input"/"select-trigger"）；L1 修复（channel 类型收敛为 string）；L5 修复（BenefitTab 两处 useEffect 补 void load() 风格统一）；L2/L3/L4 记录为 known issue。修复中一次 Edit 引入 3 处损坏（表头列丢失/垃圾行/错误 data-slot 值），已用 python3 脚本修正并构建复验通过

### 6. 产物替换与回退保障
- [x] `npm run build` 产出新 dist/ 并整体替换提交（此为 dist 唯一提交点；任务 1 构建产物仅本地验证，不入库）
- [x] 替换前确认旧实现已提交（git 基线可见），迁移失败可直接 revert dist 与前端源码
- [x] 既有 test_admin_ui.py 中断言旧 dist 文件名 / 旧 content-type 的用例随产物替换同步修订
- 验证：dist/ 为新产物且自洽（index.html 引用的资源全部存在于 dist/）✅；`python -m pytest test_admin_ui.py` 38 passed ✅
- 备注：test_admin_ui.py 断言改为从 html 正则提取 `/admin/assets/*.[js|css]` 路径后请求（不再硬编码文件名）；403 用例改 assets/ 路径；admin_ui.py 同步两处适配——①`_serve_admin_asset` 改为统一从 dist/ 服务（旧实现 index.html 取源目录文件，Vite 构建后会注入 assets 引用，源文件不含，测试暴露后修正）+ 缓存分级 assets/ 前缀 immutable（M1 修复：其余一律 no-store，防未来 public/ 无 hash 文件误入长缓存）；②`admin_asset` content-type 扩展 .mjs/.json/.map/.svg/.woff2/.woff
- 备注：任务 6 CR 修复——M2（test 补 Cache-Control 断言：index.html no-store + js immutable）；M3（admin_page docstring 更新为对齐 Vite base 前缀语义）；L1（import re 并入顶部）；L2（endswith 元组风格统一）；L3（svg 响应加 Content-Security-Policy: script-src 'none' 收口顶层导航 XSS 面）；L4/L5 记录为 known issue；修复后 pytest 38 passed 复验

### 7. 服务端 content-type 适配
- [x] `admin_ui.py` `admin_asset` 扩展 content-type（.woff2/.svg/.map/.json 等），支持 dist/assets/ 子目录
- [x] 更新/补充 `test_admin_ui.py` 静态资源用例
- 验证：`python -m pytest test_admin_ui.py` 通过（38 passed）✅；启动 server 后 `/admin/` 与全部静态资源 200 ✅（Flask test_client 冒烟：html + 4 js + 1 css 全部 200 且 content-type 正确；dist 现有扩展名 .css/.html/.js 全部已映射，woff2/svg/map/json 分支为前瞻性覆盖）
- 备注：主体适配已在任务 6 完成（content-type 映射 + assets/ 子目录 + 缓存分级 + 测试修订），本任务仅做核验与冒烟记录，无新代码变更

### 8. 集成验证与文档
- [x] 启动 server 冒烟：登录 → 五页签逐一操作（刷新模型、签到、保存配置、secrets 掩码回写、翻页日志）
- [x] 归档时按 delta spec 更新 `openspec/specs/admin-ui/spec.md`（MODIFIED「管理页面技术实现」）
- [ ] CLAUDE.md 中 admin-ui 构建描述如有出入则同步
- 验证：视觉核对清单逐项通过（jade-teal token 变量与 codex2cc index.css 一致、侧边栏 264px/64px 折叠宽度、light/dark/移动端三态人工比对 `../codex2cc/` 运行截图/界面）；旧 styles.css 无引用残留
- 备注：API 冒烟已完成（登录 401 门禁 → 登录 → overview/models/models-refresh/benefit/usage/config/credit 全端点 200 + 错误分支 400，mock 注入数据面）；spec 已按 delta 归档（MODIFIED 块整体替换，6 个 Requirement 结构完整）；全部变更已提交 5faa921（含 dist 唯一提交点）；CLAUDE.md 同步与视觉三态人工核对（核对人=用户）待收尾

## 验收标准

- 五页签功能与迁移前等价（相同端点、相同数据展示、相同文案语义）
- 视觉对齐清单全部通过：① index.css 中 jade-teal token 变量名/值与 codex2cc 一致；② 侧边栏宽度 264px（折叠 64px）；③ light / dark / 移动端三态与 codex2cc 界面人工比对一致（核对人：用户）
- 普通用户运行 server 零 Node 依赖；`npm run build` 可复现 dist/；迁移前后 dist 总字节数对比已记录
- `python -m pytest test_admin_ui.py` 全部通过
