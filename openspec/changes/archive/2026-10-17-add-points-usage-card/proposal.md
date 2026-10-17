# 变更提案：add-points-usage-card

## 背景
admin 管理页现有「免费额度」卡片只展示 token 额度，未覆盖积分（credit）维度。用户在华为云工作台看到积分用量（已用 972.77 / 总量 7500.00 / 剩余 6527.23 + 紫色进度条），希望在 admin 页同样能看到并可一键刷新。

## 数据源（已实测验证）
- 接口：`GET https://devcloud.cn-north-4.huaweicloud.com/chat/snap-manager/v1/package/overview`
- 认证：不吃 AK/SK HMAC 签名，唯一必需凭据为 cookie `devclouddevuibjJ_SESSION_ID`（名称精确大小写如前；缺失时返回 200 + 空体 0 字节）
- 展示字段：`all_credit_package.package_credit_amount`（总量）、`package_credit_used`（已用）、`package_credit_remain`（剩余）
- 展示形式：仿现有免费额度卡片，含数值三列 + 紫色进度条 + 刷新按钮

## 目标范围
**在范围内：**
- config.example.json 补充配置键 `CODEARTS_SNAP_SESSION`（存放 JSESSIONID 值，留空；config_store 为通用 KV，无需改其代码）
- 新增后端积分代理端点 `GET /api/admin/credit`（admin 鉴权保护），转发请求到上述接口并返回归一化 JSON；会话失效（200+空体、非 JSON、合法 JSON 但缺 `all_credit_package` 或三字段不全）与非 200/超时分别返回 502 + 区分性中文报错；未配置 session 返回 400 引导文案
- admin 配置页 secrets 回写：`CODEARTS_SNAP_SESSION` 纳入 `READONLY_SECRET_KEYS`（掩码展示），并在 `admin_set_config` PUT 新增 secrets 更新分支，用户可在页面更新 session 而不必手改 config.json
- admin 页新增「积分用量」卡片：总量 / 已用 / 剩余 三列数值 + 紫色进度条 + 刷新按钮（仿 :406 免费额度卡片模式）；进度条百分比计算对 total≤0 或非数字字段兜底展示 `--`
- 单元测试覆盖端点归一化、字段缺失分支与 session 失效场景

**不在范围内：**
- 不做 basic_package / bonus_credit_package / ondemand 分包明细展示（只展示聚合的 all_credit_package）
- 不做 session 自动续期 / 登录流程（session 过期由用户在 admin 配置页或手改 config.json 更新）
- 不改动 benefit.py 签到链路

## 技术方案
- 配置读取统一走 `config_store.get("CODEARTS_SNAP_SESSION")`
- 后端端点 `GET /api/admin/credit`，挂在 admin blueprint 下（复用现有 `before_request` 统一鉴权），用 requests 带 Cookie 转发，超时 15s；前端仅手动刷新，不做轮询
- 归一化在后端完成：成功时仅返回 `{amount, used, remain}` 三字段
- 失效分支区分两类：HTTP 200 且响应体为空 / 非 JSON → 502 +「积分会话已过期，请更新 CODEARTS_SNAP_SESSION」；JSON 合法但缺 `all_credit_package` 或三字段不全 → 502 +「积分接口响应结构异常，可能上游已变更」；上游非 200 / 超时 → 502 +「积分接口请求失败（网络或上游错误）」
- 配置页 secrets 回写通路：现有 `admin_set_config` PUT 只接受 `EDITABLE_KEYS` 并显式拒绝密钥键（admin_ui.py:295-313），需新增独立的 secrets 更新分支——`CODEARTS_SNAP_SESSION` 加入 `READONLY_SECRET_KEYS` 做掩码展示（复用 `_mask`），PUT 接受该键的新值并写回 config.json；提交值等于掩码输出或纯 `*` 串时跳过，不覆盖真实 session；键名保留 `READONLY_SECRET_KEYS` 原名（页面语义仍为「掩码展示、按需更新」，避免连带改动现有 AK/SK 展示逻辑）

## 预期影响
- 不影响现有聊天代理、Anthropic 端点、签到链路
- admin 页新增一个卡片与配置页一个掩码键，无破坏性改动
- config.json 新增可选键，未配置时卡片显示引导文案而非报错

## 风险
- session 值会过期：admin 配置页可掩码查看并更新该键 + 明确报错文案 + 刷新按钮可重试
- 上游限流/瞬时不稳定：超时与非 200 统一转 502 + 中文错误
- 凭据安全：session 值只存 config.json（0600 权限），配置页掩码展示，不写入代码/文档明文；Cookie 与 PUT 请求体不写入任何日志/异常堆栈
- 前提依赖：`ADMIN_PASSWORD` 未配置时 admin 面整体未启用，积分卡片与 session 更新入口均不可用，用户只能手改 config.json（README 注明）
- 进度条除零/NaN：total≤0 或非数字时前端展示 `--`，不渲染百分比
