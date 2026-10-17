# -*- coding: utf-8 -*-
"""
admin 管理界面（自建适配版）
============================
为 codearts2cc 单账号本地代理提供内嵌管理面：概览 / 模型 / 额度 / 配置
四个页签，口令鉴权（env ADMIN_PASSWORD），配置编辑写回 .env。

安全约束：
- ADMIN_PASSWORD 未设置时，/admin 返回 403、/api/admin/* 统一拒绝（管理面未启用）
- AK/SK 只读脱敏展示、禁止编辑、日志不回显完整值
- .env 仅按行精准编辑白名单键（映射 / 缓存开关），键不存在拒绝写
- admin 蓝图豁免全局 CORS（不设 Access-Control-Allow-Origin: *）

代码数据面（由 server.py configure() 注入）：
- 模型列表 / 详情 / 强制刷新
- 通道模型集合（AGENT_MODELS 引用）
- benefit 签到 / 状态 / 余额
- .env 路径与上游端点常量
"""
import hmac
import os
import shutil
import threading
import time

from flask import Blueprint, Response, jsonify, request, session

# ---------------------------------------------------------------------------
# 依赖注入接口（server.py 注册时填充；禁止延迟 import server）
# ---------------------------------------------------------------------------

admin_password = ""            # env ADMIN_PASSWORD
model_list_getter = None       # () -> list[str]，返回 SUPPORTED_MODELS
model_details_getter = None    # () -> dict，返回 MODEL_DETAILS
agent_models_ref = None        # AGENT_MODELS 列表引用
refresh_models_fn = None       # _refresh_models() -> bool（强制刷新）
benefit_balance_fn = None      # benefit.balance() -> dict
benefit_status_fn = None       # benefit.claim_status() -> dict
benefit_claim_fn = None        # benefit.claim() -> dict
usage_log_query_fn = None      # usage_log.query(page, limit) -> dict
env_path = ""                  # .env 绝对路径
upstream_endpoint = ""         # 上游聊天补全端点
listen_host = "127.0.0.1"
listen_port = 8787
model_cache_path = ""          # models-cache.json 绝对路径

# 登录失败退避：进程内内存态（threaded=True，需线程锁）
_fail_lock = threading.Lock()
_fail_count = 0
_fail_until = 0.0
_MAX_FAILURES = 5
_BACKOFF_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 900.0   # 指数退避上限 15 分钟
_lockout_rounds = 0            # 连续触发退避的轮数（指数基数）
_env_write_lock = threading.Lock()  # .env 读-改-写互斥（threaded=True 并发保护）

# 配置编辑白名单键（仅这两键可写；AK/SK 只读且禁止编辑）
EDITABLE_KEYS = ("CODEARTS_ANTHROPIC_MODEL_MAP", "CODEARTS_CACHE_SIM")
READONLY_SECRET_KEYS = ("CODEARTS_AK", "CODEARTS_SK")


def configure(*, password, model_list, model_details, agent_models,
              refresh_models, benefit_balance, benefit_status, benefit_claim,
              env_file, upstream, host, port, cache_path, usage_log_query=None):
    """由 server.py 启动时调用，注入数据面依赖。"""
    global admin_password
    global model_list_getter, model_details_getter, agent_models_ref
    global refresh_models_fn, benefit_balance_fn, benefit_status_fn, benefit_claim_fn
    global usage_log_query_fn
    global env_path, upstream_endpoint, listen_host, listen_port, model_cache_path
    admin_password = password
    model_list_getter = model_list
    model_details_getter = model_details
    agent_models_ref = agent_models
    refresh_models_fn = refresh_models
    benefit_balance_fn = benefit_balance
    benefit_status_fn = benefit_status
    benefit_claim_fn = benefit_claim
    usage_log_query_fn = usage_log_query
    env_path = env_file
    upstream_endpoint = upstream
    listen_host = host
    listen_port = port
    model_cache_path = cache_path


blueprint = Blueprint("admin_ui", __name__)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    """管理面是否启用：口令已配置即启用。"""
    return bool(admin_password)


def _logged_in() -> bool:
    """当前请求是否已建立管理会话。"""
    return bool(_enabled() and session.get("admin_ok"))


def _check_backoff() -> bool:
    """登录退避：连续多次失败后短暂拒绝登录尝试。"""
    with _fail_lock:
        if _fail_until > time.monotonic():
            return True
        return False


def _record_failure() -> None:
    """记录一次登录失败，连续失败超过阈值后指数退避（上限 15 分钟）。"""
    global _fail_count, _fail_until, _lockout_rounds
    with _fail_lock:
        _fail_count += 1
        if _fail_count >= _MAX_FAILURES:
            delay = min(_BACKOFF_SECONDS * 2 ** _lockout_rounds, _BACKOFF_MAX_SECONDS)
            _lockout_rounds += 1
            _fail_until = time.monotonic() + delay
            _fail_count = 0


def _record_success() -> None:
    global _fail_count, _fail_until, _lockout_rounds
    with _fail_lock:
        _fail_count = 0
        _fail_until = 0.0
        _lockout_rounds = 0


def _json_error(message: str, status: int) -> Response:
    return jsonify({"error": message}), status


@blueprint.before_request
def _admin_gate():
    """admin 蓝图全局门禁：未启用 403、未登录 401。"""
    path = request.path
    if path == "/admin" or path.startswith("/api/admin"):
        if not _enabled():
            return _json_error("管理界面未启用：请先在 .env 设置 ADMIN_PASSWORD", 403)
        if path == "/api/admin/login" or path == "/admin":
            return None  # 登录接口与 HTML 壳（无数据）放行，登录态由前端探测
        if not _logged_in():
            return _json_error("未登录", 401)


@blueprint.route("/admin")
def admin_page():
    """内嵌单文件管理界面。未启用时由 before_request 拦截返回 403。"""
    return Response(_ADMIN_HTML, status=200, content_type="text/html; charset=utf-8")


@blueprint.route("/api/admin/login", methods=["POST"])
def admin_login():
    """口令登录：成功建会话，失败退避拦截 + 记录失败。"""
    if not _enabled():
        return _json_error("管理界面未启用", 403)
    if _check_backoff():
        return _json_error("尝试次数过多，请稍后再试", 429)
    data = request.get_json(silent=True) or {}
    supplied = str(data.get("password") or "").encode("utf-8")
    if not hmac.compare_digest(supplied, admin_password.encode("utf-8")):
        _record_failure()
        return _json_error("口令错误", 401)
    _record_success()
    session["admin_ok"] = True
    return jsonify({"ok": True})


@blueprint.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_ok", None)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 概览 / 模型 / 额度 / 配置
# ---------------------------------------------------------------------------

@blueprint.route("/api/admin/overview", methods=["GET"])
def admin_overview():
    agent = list(agent_models_ref or [])
    all_models = model_list_getter() if model_list_getter else []
    benefit_count = len([m for m in all_models if m not in agent])
    cache_sim = os.getenv("CODEARTS_CACHE_SIM", "1").strip() != "0"
    try:
        cache_mtime = os.path.getmtime(model_cache_path)
    except OSError:
        cache_mtime = 0
    return jsonify({
        "listen": f"{listen_host}:{listen_port}",
        "listen_host": listen_host,
        "public_exposed": listen_host not in ("127.0.0.1", "localhost", "::1"),
        "upstream": upstream_endpoint,
        "model_count": len(all_models),
        "agent_count": len(agent),
        "benefit_count": benefit_count,
        "cache_sim_enabled": cache_sim,
        "cache_updated_at": int(cache_mtime) if cache_mtime else 0,
    })


@blueprint.route("/api/admin/models", methods=["GET"])
def admin_models():
    agent = set(agent_models_ref or [])
    details = model_details_getter() if model_details_getter else {}
    models = model_list_getter() if model_list_getter else []
    groups = {"agent": [], "benefit": []}
    for model_id in models:
        item = details.get(model_id) or {"id": model_id, "name": model_id}
        channel = "agent" if model_id in agent else "benefit"
        groups[channel].append({
            "id": item.get("id") or model_id,
            "name": item.get("name") or model_id,
            "channel": channel,
            "context_window": item.get("context_window"),
            "max_tokens": item.get("max_tokens"),
            "supports_images": bool(item.get("supports_images", False)),
        })
    return jsonify(groups)


@blueprint.route("/api/admin/models/refresh", methods=["POST"])
def admin_refresh_models():
    ok = refresh_models_fn() if refresh_models_fn else False
    return jsonify({"ok": bool(ok)}), (200 if ok else 502)


@blueprint.route("/api/admin/usage", methods=["GET"])
def admin_usage():
    """使用日志查询：统计 + 明细分页（page 默认 1 / limit 默认 50 上限 200，时间倒序）。"""
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1
    try:
        limit = int(request.args.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    page = max(1, page)
    limit = min(max(1, limit), 200)
    return jsonify(usage_log_query_fn(page=page, limit=limit))


@blueprint.route("/api/admin/benefit", methods=["GET"])
def admin_benefit():
    try:
        balance = benefit_balance_fn() if benefit_balance_fn else {}
        status = benefit_status_fn() if benefit_status_fn else {}
    except Exception as error:  # noqa: BLE001
        return _json_error(f"查询额度失败: {error}", 502)
    return jsonify({
        "balance": balance or {},
        "status": status or {},
        "total_quota": (balance or {}).get("total_quota") or 0,
        "total_balance": (balance or {}).get("total_balance") or 0,
        "used_amount": (balance or {}).get("used_amount") or 0,
        "last_claim_ms": (status or {}).get("create_time") or 0,
    })


@blueprint.route("/api/admin/benefit/claim", methods=["POST"])
def admin_claim():
    try:
        result = benefit_claim_fn() if benefit_claim_fn else {}
    except Exception as error:  # noqa: BLE001
        return _json_error(f"签到失败: {error}", 502)
    return jsonify({"ok": True, "status": result or {}})


# ---------------------------------------------------------------------------
# 配置（.env 按行精准编辑）
# ---------------------------------------------------------------------------

def _mask(value: str) -> str:
    """脱敏：仅保留首尾各 2 字符。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _read_env_lines() -> list:
    """返回 .env 全部行（不存在时返回空列表）。"""
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            return handle.readlines()
    except OSError:
        return []


def _parse_env_line(line: str):
    """解析一行 env；返回 (key, value, 原始行) 或 None（非键值行）。"""
    stripped = line.rstrip("\r\n")
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    value = value.strip()
    value = value.strip("'\"")
    return key, value, stripped


def _read_env_map() -> dict:
    """读取 .env 中全部键值（键 → 值）。"""
    result = {}
    for line in _read_env_lines():
        parsed = _parse_env_line(line)
        if parsed:
            result[parsed[0]] = parsed[1]
    return result


def _write_env_value(key: str, new_value: str) -> bool:
    """按行精准更新 .env 中指定键的值；键不存在返回 False。

    仅当 key 在文件中存在时替换 `=` 后的值部分，保留原行其余字节
    （前导空白、行尾注释、CRLF 均保留）。原子写（tmp + os.replace）
    防止写一半截断；线程锁串行化读-改-写，避免并发 PUT 丢更新。
    """
    with _env_write_lock:
        lines = _read_env_lines()
        found = False
        for index, line in enumerate(lines):
            parsed = _parse_env_line(line)
            if parsed and parsed[0] == key:
                # 保留 `key` 前缀原文（含前导空白与 key 大小写拼写）
                eq_pos = line.rstrip("\r\n").index("=")
                head = line[:eq_pos + 1]
                tail = line.rstrip("\r\n")[eq_pos + 1:]
                # 仅替换值，保留尾部（若有行内注释则一并保留）
                comment = ""
                if " #" in tail:
                    _, _, comment = tail.partition(" #")
                    comment = " #" + comment
                lines[index] = head + new_value + comment + ("\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "")
                found = True
                break
        if not found:
            return False
        tmp_path = env_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        shutil.copymode(env_path, tmp_path)  # os.replace 换 inode，需显式保留原文件权限
        os.replace(tmp_path, env_path)
    os.environ[key] = new_value
    return True


@blueprint.route("/api/admin/config", methods=["GET"])
def admin_get_config():
    env_map = _read_env_map()
    editable = {key: env_map.get(key, "") for key in EDITABLE_KEYS}
    secrets = {key: _mask(env_map.get(key, "")) for key in READONLY_SECRET_KEYS}
    return jsonify({"editable": editable, "secrets": secrets})


@blueprint.route("/api/admin/config", methods=["PUT"])
def admin_set_config():
    data = request.get_json(silent=True) or {}
    edits = data.get("editable") or {}
    if not isinstance(edits, dict):
        return _json_error("editable 必须为对象", 400)
    # 只接受白名单键；拒绝任何密钥键（即使拼写类似）
    for key in edits:
        if key not in EDITABLE_KEYS:
            return _json_error(f"不支持编辑配置键: {key}", 400)
    results = {}
    for key, value in edits.items():
        # 值必须是单行可打印字符串：杜绝换行注入 .env 伪造键行（如 ADMIN_PASSWORD=）
        if (not isinstance(value, str) or not value.isprintable()
                or "\n" in value or "\r" in value):
            return _json_error(f"配置键 {key} 的值必须为单行可打印字符串", 400)
        if not _write_env_value(key, value):
            return _json_error(
                f"配置键 {key} 不存在于 .env，请先手动添加后再编辑", 400
            )
        results[key] = value
    return jsonify({"ok": True, "updated": results})


# ---------------------------------------------------------------------------
# 前端单文件（原生 HTML+CSS+JS，跟随 prefers-color-scheme）
# ---------------------------------------------------------------------------

_ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CodeArts 代理管理</title>
<style>
:root{--bg:#fafafa;--fg:#1a1a1a;--muted:#666;--card:#fff;--line:#e5e5e5;--accent:#0b6bcb}
@media (prefers-color-scheme: dark){:root{--bg:#141414;--fg:#e5e5e5;--muted:#999;--card:#1f1f1f;--line:#333;--accent:#4a9df8}}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:16px;margin:0}
nav{display:flex;gap:4px;padding:12px 20px;border-bottom:1px solid var(--line)}
nav button{border:1px solid var(--line);background:var(--card);color:var(--fg);padding:8px 14px;border-radius:6px;cursor:pointer}
nav button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
/* 内容区与页头对齐：页头全宽、左右内边距 20px，内容区同样全宽铺满，
   左缘对齐「CodeArts 代理管理」文案、右缘对齐「退出登录」按钮 */
main{padding:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin-bottom:16px}
.muted{color:var(--muted)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px}
.badge.agent{background:#e3f2fd;color:#0b6bcb}.badge.benefit{background:#e8f5e9;color:#2e7d32}
@media (prefers-color-scheme:dark){.badge.agent{background:#0b2a4a;color:#4a9df8}.badge.benefit{background:#15321a;color:#66bb6a}}
button.primary{background:var(--accent);color:#fff;border:none;padding:8px 16px;border-radius:6px;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
input[type=password],input[type=text]{width:100%;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);margin:8px 0}
.warn{background:#fff3e0;border:1px solid #e6a23c;color:#8a5a00;border-radius:6px;padding:10px 14px;margin:10px 0}
@media (prefers-color-scheme:dark){.warn{background:#2a2010;color:#e6a23c}}
#logout{background:transparent;border:1px solid var(--line);color:var(--fg);padding:6px 12px;border-radius:6px;cursor:pointer}
.hidden{display:none}
.stats{display:flex;gap:16px;flex-wrap:wrap}
.stat{flex:1;min-width:120px;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:12px;text-align:center}
.stat b{display:block;font-size:20px;margin-bottom:4px}
.stat span{font-size:12px;color:var(--muted)}
.badge.stream{background:#e8f5e9;color:#2e7d32}
.badge.plain{background:var(--bg);color:var(--muted)}
.badge.openai{background:#e3f2fd;color:#1565c0}
.badge.anthropic{background:#f3e5f5;color:#6a1b9a}
.pager{margin-top:10px;display:flex;gap:8px}
.hbtn{font-size:12px;font-weight:400;padding:4px 12px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;cursor:pointer;vertical-align:middle;margin-left:8px}
.muted{color:var(--muted)}
@media (prefers-color-scheme:dark){
.badge.stream{background:#1b2e1b;color:#81c784}
.badge.openai{background:#12253d;color:#64b5f6}
.badge.anthropic{background:#2b1a33;color:#ba68c8}
}
</style>
</head>
<body>
<header><h1>CodeArts 代理管理</h1><span id="logoutWrap" class="hidden"><button id="logout">退出登录</button></span></header>
<div id="loginPage">
  <main><div class="card"><h2>登录</h2><input type="password" id="pw" placeholder="管理口令"><button class="primary" id="loginBtn">登录</button><div id="loginErr" class="muted"></div></div></main>
</div>
<div id="appPage" class="hidden">
  <nav>
    <button data-tab="overview" class="active">概览</button>
    <button data-tab="models">模型</button>
    <button data-tab="benefit">额度</button>
    <button data-tab="usage">使用日志</button>
    <button data-tab="config">配置</button>
  </nav>
  <main id="main"></main>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=(url,opts={})=>fetch(url,{headers:{'Content-Type':'application/json'},...opts}).then(async r=>{const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||('HTTP '+r.status));return d});
const show=el=>{el.classList.remove('hidden')},hide=el=>{el.classList.add('hidden')};
let authed=false;
function checkAuth(){return api('/api/admin/overview').then(()=>{authed=true;showApp()}).catch(()=>{authed=false;showLogin()})}
function showLogin(){authed=false;show($('#loginPage'));hide($('#appPage'));hide($('#logoutWrap'))}
function showApp(){show($('#appPage'));hide($('#loginPage'));show($('#logoutWrap'));loadTab('overview')}
$('#loginBtn').onclick=()=>{api('/api/admin/login',{method:'POST',body:JSON.stringify({password:$('#pw').value})}).then(()=>{checkAuth()}).catch(e=>{$('#loginErr').textContent=e.message})};
$('#logout').onclick=()=>{api('/api/admin/logout',{method:'POST'}).finally(showLogin)};
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');loadTab(b.dataset.tab)});
function loadTab(tab){
  const m=$('#main');
  if(tab==='overview')api('/api/admin/overview').then(d=>{let warn=d.public_exposed?'<div class="warn">⚠️ 监听地址非回环，管理面已暴露公网</div>':'';m.innerHTML=`<div class="card"><h2>概览</h2>${warn}<table><tr><th>监听</th><td>${esc(d.listen)}</td></tr><tr><th>上游</th><td>${esc(d.upstream)}</td></tr><tr><th>模型数</th><td>${d.model_count}（agent ${d.agent_count} / benefit ${d.benefit_count}）</td></tr><tr><th>缓存模拟</th><td>${d.cache_sim_enabled?'开启':'关闭'}</td></tr></table></div>`}).catch(e=>m.textContent=e.message);
  if(tab==='models'){
    m.innerHTML='<div id="mwrap">加载中…</div><button class="primary" id="refresh">强制刷新</button>';
    const load=()=>api('/api/admin/models').then(d=>{let rows=(c)=>c.map(x=>`<tr><td>${esc(x.id)}</td><td>${esc(x.name)}</td><td><span class="badge ${esc(x.channel)}">${esc(x.channel)}</span></td><td>${esc(x.context_window??'-')}</td><td>${esc(x.max_tokens??'-')}</td><td>${x.supports_images?'是':'否'}</td></tr>`).join('');$('#mwrap').innerHTML=`<table><tr><th>ID</th><th>名称</th><th>通道</th><th>上下文</th><th>最大输出</th><th>图片</th></tr><tr><th colspan=6 class="muted">agent</th></tr>${rows(d.agent)}<tr><th colspan=6 class="muted">benefit</th></tr>${rows(d.benefit)}</table>`}).catch(e=>$('#mwrap').textContent=e.message);
    load();$('#refresh').onclick=()=>api('/api/admin/models/refresh',{method:'POST'}).then(load).catch(e=>$('#mwrap').textContent=e.message);
  }
  if(tab==='benefit'){
    const load=()=>api('/api/admin/benefit').then(d=>{const t=d.total_quota,b=d.total_balance,u=d.used_amount;m.innerHTML=`<div class="card"><h2>免费额度</h2><table><tr><th>总配额</th><td>${esc(t)}</td></tr><tr><th>剩余</th><td>${esc(b)}</td></tr><tr><th>已用</th><td>${esc(u)}</td></tr></table><button class="primary" id="claim">立即签到</button><div id="claimMsg" class="muted"></div></div>`;$('#claim').onclick=()=>{const btn=$('#claim');btn.disabled=true;api('/api/admin/benefit/claim',{method:'POST'}).then(r=>{btn.disabled=false;$('#claimMsg').textContent='签到成功';load()}).catch(e=>{btn.disabled=false;$('#claimMsg').textContent=e.message})}}).catch(e=>m.textContent=e.message);
    load();
  }
  if(tab==='usage'){
    const fmt=n=>(n??0).toLocaleString();
    const fmtMs=ms=>ms==null?'-':(ms>=1000?(ms/1000).toFixed(1)+'s':ms+'ms');
    const fmtTs=ts=>esc(String(ts||'').replace('T',' '));
    const load=(page=1)=>{
      api('/api/admin/usage?page='+page+'&limit=50').then(d=>{
        const s=d.summary||{};
        const groups=Object.entries(d.by_model||{}).map(([mo,g])=>`<tr><td>${esc(mo)}</td><td>${fmt(g.requests)}</td><td>${fmt(g.input_tokens)}</td><td>${fmt(g.output_tokens)}</td><td>${fmt(g.cache_read_tokens)}</td></tr>`).join('');
        const rows=(d.entries||[]).map(e=>{
          const total=(e.input_tokens||0)+(e.cache_read_tokens||0);
          const tags=`<span class="badge ${e.stream?'stream':'plain'}">${e.stream?'流式':'非流式'}</span>${e.effort?` <span class="badge plain">${esc(e.effort)}</span>`:''}`;
          return `<tr><td>${fmtTs(e.ts)}</td><td>${esc(e.ip||'-')}</td><td><span class="badge ${esc(e.endpoint)}">${esc(e.endpoint)}</span></td><td>${esc(e.model)}</td><td>${tags}</td><td>${fmt(e.input_tokens)}</td><td>${fmt(e.output_tokens)}</td><td>${fmt(e.cache_read_tokens)}</td><td>${fmt(total)}</td><td>${fmtMs(e.first_token_ms)}</td><td>${fmtMs(e.duration_ms)}</td></tr>`;
        }).join('');
        const total=d.total||0,pages=Math.max(1,Math.ceil(total/(d.limit||50)));
        m.innerHTML=`<div class="card"><h2>使用统计 <button id="uRefresh" class="hbtn">刷新</button></h2><div class="stats"><div class="stat"><b>${fmt(s.requests)}</b><span>总请求数</span></div><div class="stat"><b>${fmt(s.input_tokens)}</b><span>输入 Tokens</span></div><div class="stat"><b>${fmt(s.output_tokens)}</b><span>输出 Tokens</span></div><div class="stat"><b>${fmt(s.cache_read_tokens)}</b><span>缓存读取 Tokens</span></div></div></div>
        ${groups?`<div class="card"><h2>按模型分组</h2><table><tr><th>模型</th><th>请求数</th><th>输入</th><th>输出</th><th>缓存读取</th></tr>${groups}</table></div>`:''}
        <div class="card"><h2>请求明细 <span class="muted">${total} 条，第 ${d.page}/${pages} 页</span></h2><table><tr><th>时间</th><th>IP</th><th>端点</th><th>模型</th><th>标签</th><th>输入</th><th>输出</th><th>缓存读取</th><th>输入总计</th><th>首字</th><th>总耗时</th></tr>${rows||'<tr><td colspan=11 class="muted">暂无记录</td></tr>'}</table>
        <div class="pager"><button id="uPrev" ${d.page<=1?'disabled':''}>上一页</button><button id="uNext" ${d.page>=pages?'disabled':''}>下一页</button></div></div>`;
        $('#uPrev').onclick=()=>load(d.page-1);
        $('#uNext').onclick=()=>load(d.page+1);
        $('#uRefresh').onclick=()=>load(d.page);
      }).catch(e=>m.textContent=e.message);
    };
    load();
  }
  if(tab==='config'){
    const load=()=>api('/api/admin/config').then(d=>{const ed=d.editable;const sk=d.secrets;m.innerHTML=`<div class="card"><h2>配置</h2><label>模型映射 CODEARTS_ANTHROPIC_MODEL_MAP</label><input id="cfgMap" value="${esc(ed.CODEARTS_ANTHROPIC_MODEL_MAP||'')}" placeholder="alias:model,..."><label>缓存模拟开关 CODEARTS_CACHE_SIM</label><select id="cfgCache"><option value="1" ${ed.CODEARTS_CACHE_SIM!=='0'?'selected':''}>开启</option><option value="0" ${ed.CODEARTS_CACHE_SIM==='0'?'selected':''}>关闭</option></select><button class="primary" id="saveCfg">保存</button><div id="cfgMsg" class="muted"></div><hr><h3>密钥（只读脱敏）</h3><table><tr><th>CODEARTS_AK</th><td>${esc(sk.CODEARTS_AK||'(未配置)')}</td></tr><tr><th>CODEARTS_SK</th><td>${esc(sk.CODEARTS_SK||'(未配置)')}</td></tr></table></div>`;$('#saveCfg').onclick=()=>{$('#saveCfg').disabled=true;api('/api/admin/config',{method:'PUT',body:JSON.stringify({editable:{CODEARTS_ANTHROPIC_MODEL_MAP:$('#cfgMap').value,CODEARTS_CACHE_SIM:$('#cfgCache').value}})}).then(r=>{$('#saveCfg').disabled=false;$('#cfgMsg').textContent='已保存（立即生效）'}).catch(e=>{$('#saveCfg').disabled=false;$('#cfgMsg').textContent=e.message})}}).catch(e=>m.textContent=e.message);
    load();
  }
}
checkAuth();
</script>
</body>
</html>
"""