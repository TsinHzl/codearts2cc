## MODIFIED Requirements

### Requirement: 管理页面技术实现
管理页前端由 React（19）+ TypeScript 实现：源码 TSX 经 Vite 预构建（`base: '/admin/'`，产物提交入库含 package-lock.json），运行 server 零 Node 依赖；样式采用 Tailwind CSS 4 token 层（`@theme` jade-teal 色板 + `@custom-variant dark`），布局为 codex2cc 风格侧边栏体系（桌面折叠侧栏 + 移动端 topbar/bottom nav），视觉完全对齐 codex2cc 管理界面；概览/模型/额度/配置四个页签的布局、数据来源与交互语义保持一致，使用日志页的布局与交互以 `usage-log` capability 规格为准；静态资源由 before_request 门禁覆盖（未配置口令时 403），assets/ 带 contenthash 长 immutable 缓存、其余 no-store；禁止 `dangerouslySetInnerHTML`。

#### Scenario: 页签功能等价
- **WHEN** 已登录用户切换概览/模型/额度/配置页签
- **THEN** 各页签调用与原实现相同的 `/api/admin/*` 端点，展示相同数据结构与文案（含积分卡片三分支、secrets 掩码回写跳过行为）

#### Scenario: 重新构建产物
- **WHEN** 开发者在 `admin-ui/` 执行 `npm install && npm run build`
- **THEN** Vite 以 devDependencies 固定版本产出 dist 产物（含 hash 文件名与 assets/ 子目录），提交后普通用户运行 server 无需 Node

#### Scenario: 视觉对齐 codex2cc
- **WHEN** 用户访问管理界面（light 或 dark 模式、桌面或移动端视口）
- **THEN** 布局结构（侧边栏/移动端导航）与主题配色与 codex2cc admin 一致

#### Scenario: 使用日志页展示对齐
- **WHEN** 用户打开使用日志页
- **THEN** 页头显示面包屑「主要 / 使用日志」、标题「使用日志」、副标题「按时间维度查看用量与请求明细」
- **AND** 时间列固定 `YYYY/MM/DD HH:mm:ss`，费用带 `$` 前缀且固定 4 位小数，KIRO CREDITS 固定 4 位小数并标注「积分」
- **AND** 明细「输入总计」= 输入 + 缓存读取；「用时」无值显示「–」
- **AND** 请求日志默认每页 100 条；统计卡副行文案符合截图口径；环形占比 = 缓存读取 ÷ 输入总计（输入 + 缓存读取），保留一位小数

#### Scenario: 静态资源类型覆盖
- **WHEN** 浏览器请求 Vite 产出的 `.js`/`.css`/`.woff2`/`.svg`/`.map` 等静态资源
- **THEN** 系统按正确 content-type 返回，未登录时被门禁拦截，assets/ 产物长 immutable 缓存、index.html no-store

### Requirement: 运行时配置编辑
The system SHALL 允许管理员编辑模型别名映射（`CODEARTS_ANTHROPIC_MODEL_MAP`）、缓存模拟开关（`CODEARTS_CACHE_SIM`）、账号显示名（`CODEARTS_ACCOUNT_NAME`）、模型单价表（`CODEARTS_PRICING`）与 Credits 换算系数（`CODEARTS_CREDITS_RATE`），并 SHALL 将变更按行精准写回 `config.json` 文件后立即生效。配置页保持白名单键校验（EDITABLE_KEYS 单行可打印校验、SECRET_WRITABLE_KEYS 掩码/纯 `*` 回写跳过）的后端契约不变；单价表因后端单行约束，前端 SHALL 在提交前将多行 JSON 压缩为单行。配置读取接口 SHALL 对缺失的账号名、单价表与 Credits 系数返回默认值（「主账号」、`{}`、`1.0`），且账号名为空串时 SHALL 回退默认值。

#### Scenario: 编辑模型映射
- **WHEN** 管理员在配置页修改模型别名映射并保存
- **THEN** 系统按行精准更新 `config.json` 中 `CODEARTS_ANTHROPIC_MODEL_MAP` 的值
- **AND** 进程内映射配置立即生效
- **AND** `config.json` 中 AK/SK 行不被改写

#### Scenario: 切换缓存模拟开关
- **WHEN** 管理员切换 `CODEARTS_CACHE_SIM` 开关
- **THEN** 系统更新 `config.json` 中该键的值并立即生效

#### Scenario: 编辑账号显示名
- **WHEN** 管理员修改 `CODEARTS_ACCOUNT_NAME` 并保存
- **THEN** 使用日志页的账号列立即显示新名称

#### Scenario: 编辑单价表
- **WHEN** 管理员在配置页以多行 JSON 编辑 `CODEARTS_PRICING` 并保存
- **THEN** 前端将多行 JSON 压缩为单行后提交，后端通过单行可打印校验并按行写回 `config.json`
- **AND** 保存后下一次使用日志查询立即按新单价计算费用与 Credits
- **AND** 单价表每次查询只解析一次，非法配置每查询最多一条警告

#### Scenario: 拒绝非法单价表
- **WHEN** 管理员提交非合法 JSON、含负数 / `NaN` / `Infinity` / 布尔值单价或缺少 `input`/`output` 的 `CODEARTS_PRICING`
- **THEN** 系统拒绝保存并返回可读错误信息
- **AND** `config.json` 中该键保持原值不被覆盖

#### Scenario: 密钥只读保护
- **WHEN** 管理员查看配置页
- **THEN** 系统仅以脱敏形式展示 `CODEARTS_AK`/`CODEARTS_SK`
- **AND** 这些密钥 SHALL 不可通过管理界面编辑

#### Scenario: secrets 回写跳过
- **WHEN** 用户保存配置且 secrets 字段保持掩码回显值或纯 `*` 串
- **THEN** 前端原样提交、后端跳过更新，实际存储值不变

## ADDED Requirements

### Requirement: 使用日志每日统计视图
使用日志页 SHALL 提供「每日统计」切换：开启时以日聚合表替换请求日志表，关闭时恢复明细表；两种视图共享当前时间区间。

#### Scenario: 切换每日统计
- **WHEN** 用户开启「每日统计」
- **THEN** 请求日志表替换为日聚合表，列为日期 / 请求数 / 输入 / 输出 / 缓存读取 / 缓存写入 / 费用 / Credits / 节省 / 未计价条数
- **AND** 日聚合表不分页，未计价日的金额列显示「–」

#### Scenario: 空区间与切回明细
- **WHEN** 区间内无记录
- **THEN** 日聚合表显示「该区间无记录」
- **WHEN** 用户切回明细视图
- **THEN** 保留当前时间区间并将页码重置为 1

#### Scenario: 区间控件交互
- **WHEN** 用户点击自定义区间或切换预设
- **THEN** 预设高亮与自定义区间互斥，页码重置为 1

#### Scenario: 按模型分组表保留
- **WHEN** 用户查看使用日志页
- **THEN** 现有「按模型分组」表以可折叠面板呈现，默认折叠
