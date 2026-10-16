# 项目上下文

## 技术栈
Python 3 + Flask（无数据库）；requests 做上游 HTTP；python-dotenv 加载 `.env`（CODEARTS_AK / CODEARTS_SK）。

## 架构约定
单文件代理（server.py）：OpenAI Chat Completions 入口 → 华为云 SDK-HMAC-SHA256 签名 → 转发 CodeArts 上游（snap-access）。模型分 agent（老套餐）/ benefit（免费额度）双通道，签名头严格互斥；通道由模型详情 `channel` 字段驱动。上游 SSE 怪癖（finish_reason 缺失、content:null、finish_reason:"other"）在代理层修正，详见 CLAUDE.md。

## 目录结构
- `server.py` — 主服务：签名 + 转发 + 模型列表生命周期（缓存 models-cache.json，300s TTL）
- `benefit.py` — 免费额度签到/余额/模型查询 CLI
- `test_openai.py` — 对运行中服务的集成冒烟脚本（无单测框架）
- `models-cache.json` — 模型缓存（原子写）

## 开发约定
- 中文注释与用户可见错误消息；类型注解；PEP 8
- 无 lint/单测框架配置；验证靠 `python test_openai.py`（需先启动 server）
- 约束：仅绑定 127.0.0.1；代理不鉴权；不修改模型行为只做协议适配
