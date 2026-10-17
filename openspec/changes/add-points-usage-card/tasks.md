# 任务清单：add-points-usage-card

## 状态：DONE

> 执行约定（用户已确认）：在 master 当前分支直接开发，不新建分支。

## 任务
- [x] 任务 1：config.example.json 补充键 `CODEARTS_SNAP_SESSION`（留空；config_store 无需改代码）——验证：`python3 -m pytest test_config_store.py -q` 通过，且 `config_store.get("CODEARTS_SNAP_SESSION")` 对未配置键返回空串（现有行为断言）
- [x] 任务 2：新增后端积分代理端点 `GET /api/admin/credit`（admin blueprint 下，带 JSESSIONID cookie 转发 `package/overview`）：成功归一化为 `{amount, used, remain}`；200+空体 / 非 JSON → 502「积分会话已过期」；JSON 合法但缺字段 → 502「响应结构异常」；上游非 200 / 超时 → 502「请求失败（网络或上游错误）」；未配置 session → 400 引导文案；实现约束：发往上游的请求异常处理不得把 Cookie 头或请求体写入日志/异常堆栈——验证：新增单测覆盖全部失效分支（mock 上游），`python3 -m pytest test_admin_ui.py -q` 通过
- [x] 任务 3：admin 配置页 secrets 回写通路——`CODEARTS_SNAP_SESSION` 加入 `READONLY_SECRET_KEYS`（掩码展示），并改造 `admin_set_config` PUT：新增独立 secrets 更新分支接受该键写入新值（现 PUT 只接受 EDITABLE_KEYS 并拒绝密钥键）；提交值等于掩码输出或纯 `*` 串时跳过更新，不覆盖真实 session；PUT 异常处理同样不得把请求体写入日志——验证：单测断言 GET 返回掩码值、PUT 写入新值生效、PUT 提交掩码值不覆盖真实值，`python3 -m pytest test_admin_ui.py -q` 通过
- [x] 任务 4：admin 页新增「积分用量」卡片（总量/已用/剩余 + 着色进度条 + 刷新按钮，仿免费额度卡片；total≤0 或非数字时进度条展示 `--`；未配置 session 时展示引导文案）——验证：单测通过后，启动 server 手动查看 /admin 页面（手动验收项，见验收标准末条）
- [x] 任务 5：README 补充 `CODEARTS_SNAP_SESSION` 配置说明、admin 配置页更新方式、session 过期处理，并注明「ADMIN_PASSWORD 未配置时管理面整体未启用，积分卡片不可用，只能手改 config.json」——验证：README 相关节存在该键说明

## 验收标准
- [ ] 单测断言归一化输出结构为 `{amount, used, remain}`（mock 上游固定值）
- [ ] session 未配置或过期（空体/解析失败/缺字段/上游非 200/超时）时，卡片展示明确中文提示，不出现裸异常
- [ ] admin 配置页可见并可通过掩码输入更新 `CODEARTS_SNAP_SESSION`；提交掩码值不会覆盖真实 session
- [ ] Cookie 与 PUT 请求体不写入任何日志/异常堆栈（任务 2/3 的实现约束，单测或代码审查确认）
- [ ] config.json 写入路径权限保持 0600（config_store.set_many 原子写回已 `copymode`/`chmod 0600` 兜底，任务 1/3 写回路径复用该机制，不新增绕过写法）
- [ ] 全量单测 `python3 -m pytest -q` 通过
- [ ] （手动验收）真实 session 下点击刷新按钮可拉取数据并更新卡片，数值与华为云工作台一致 + 着色进度条正常渲染
