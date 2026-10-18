import { useEffect, useState } from 'react'
import { api } from '../api'

interface ConfigData {
  editable?: Record<string, string>
  secrets?: Record<string, string | undefined>
}

const inputCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30'
const labelCls = 'mb-1.5 block text-[12px] font-semibold text-muted-foreground'

const PRICE_FIELDS = ['input', 'output', 'cache_read', 'cache_creation'] as const
const REQUIRED_PRICE_FIELDS = ['input', 'output'] as const

type PricingResult = { ok: true; value: string } | { ok: false; error: string }

const isFiniteNonNegative = (value: unknown): boolean =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

function normalizePricing(text: string): PricingResult {
  if (!text.trim()) {
    return { ok: true, value: '{}' }
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    return { ok: false, error: `单价表不是合法 JSON（${detail}）` }
  }
  if (!isPlainObject(parsed)) {
    return { ok: false, error: '单价表顶层必须是对象' }
  }
  for (const [model, item] of Object.entries(parsed)) {
    if (!model) {
      return { ok: false, error: '单价表的模型键必须是非空字符串' }
    }
    if (!isPlainObject(item)) {
      return { ok: false, error: `单价表中 ${model} 的配置必须是对象` }
    }
    for (const field of REQUIRED_PRICE_FIELDS) {
      if (!(field in item)) {
        return { ok: false, error: `单价表中 ${model} 缺少必填单价 ${field}` }
      }
    }
    for (const field of PRICE_FIELDS) {
      if (field in item && !isFiniteNonNegative(item[field])) {
        return { ok: false, error: `单价表中 ${model} 的 ${field} 必须是非负有限数值` }
      }
    }
  }
  return { ok: true, value: JSON.stringify(parsed) }
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

export default function ConfigTab() {
  const [data, setData] = useState<ConfigData | null>(null)
  const [err, setErr] = useState('')
  const [cfgMsg, setCfgMsg] = useState('')
  const [snapMsg, setSnapMsg] = useState('')
  const [cfg, setCfg] = useState({
    CODEARTS_ANTHROPIC_MODEL_MAP: '',
    CODEARTS_CACHE_SIM: '1',
    CODEARTS_ACCOUNT_NAME: '',
    CODEARTS_PRICING: '{}',
    CODEARTS_CREDITS_RATE: '1.0',
    CODEARTS_THINKING_AS_TEXT: '1',
    CODEARTS_REQUIRE_API_KEY: '1',
  })
  const [snap, setSnap] = useState('')

  const load = () =>
    api<ConfigData>('/api/admin/config').then((d) => {
      setData(d)
      setCfg({
        CODEARTS_ANTHROPIC_MODEL_MAP: d.editable?.CODEARTS_ANTHROPIC_MODEL_MAP || '',
        CODEARTS_CACHE_SIM: d.editable?.CODEARTS_CACHE_SIM !== '0' ? '1' : '0',
        CODEARTS_ACCOUNT_NAME: d.editable?.CODEARTS_ACCOUNT_NAME || '',
        CODEARTS_PRICING: prettyJson(d.editable?.CODEARTS_PRICING || '{}'),
        CODEARTS_CREDITS_RATE: d.editable?.CODEARTS_CREDITS_RATE || '1.0',
        CODEARTS_THINKING_AS_TEXT: d.editable?.CODEARTS_THINKING_AS_TEXT !== '0' ? '1' : '0',
        CODEARTS_REQUIRE_API_KEY: d.editable?.CODEARTS_REQUIRE_API_KEY !== '0' ? '1' : '0',
      })
      setSnap('') // 掩码不载入输入框，仅作 placeholder 提示；提交掩码后端也会跳过
    }).catch((e) => setErr(e.message))

  useEffect(() => { void load() }, [])

  if (err && !data) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  const saveCfg = () => {
    const pricing = normalizePricing(cfg.CODEARTS_PRICING)
    if (!pricing.ok) {
      setCfgMsg(pricing.error)
      return
    }
    const rate = Number(cfg.CODEARTS_CREDITS_RATE)
    if (!cfg.CODEARTS_CREDITS_RATE.trim() || !Number.isFinite(rate) || rate < 0) {
      setCfgMsg('Credits 换算系数必须为非负有限数值')
      return
    }
    const account = cfg.CODEARTS_ACCOUNT_NAME.trim() || '主账号'
    const payload = { ...cfg, CODEARTS_ACCOUNT_NAME: account, CODEARTS_PRICING: pricing.value }
    api('/api/admin/config', {
      method: 'PUT',
      body: JSON.stringify({ editable: payload }),
    })
      .then(() => {
        setCfg((prev) => ({ ...prev, CODEARTS_ACCOUNT_NAME: account, CODEARTS_PRICING: prettyJson(pricing.value) }))
        setCfgMsg('已保存（立即生效）')
      })
      .catch((e) => setCfgMsg(e.message))
  }

  const saveSnap = () =>
    api<{ updated?: string[] }>('/api/admin/config/secrets', {
      method: 'PUT',
      body: JSON.stringify({ secrets: { CODEARTS_SNAP_SESSION: snap } }),
    })
      .then((r) => setSnapMsg(r.updated && r.updated.length ? '已保存（立即生效）' : '未变更（掩码/纯 * 串不覆盖）'))
      .catch((e) => setSnapMsg(e.message))

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
        <h2 className="mb-4 text-base font-bold text-foreground">配置</h2>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-model-map">模型映射 CODEARTS_ANTHROPIC_MODEL_MAP</label>
          <input
            id="cfg-model-map"
            type="text"
            data-slot="input"
            className={inputCls}
            value={cfg.CODEARTS_ANTHROPIC_MODEL_MAP}
            placeholder="alias:model,..."
            onChange={(e) => setCfg({ ...cfg, CODEARTS_ANTHROPIC_MODEL_MAP: e.target.value })}
          />
        </div>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-cache-sim">缓存模拟开关 CODEARTS_CACHE_SIM</label>
          <select
            id="cfg-cache-sim"
            data-slot="select-trigger"
            className={inputCls}
            value={cfg.CODEARTS_CACHE_SIM}
            onChange={(e) => setCfg({ ...cfg, CODEARTS_CACHE_SIM: e.target.value })}
          >
            <option value="1">开启</option>
            <option value="0">关闭</option>
          </select>
        </div>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-thinking-as-text">思考文本化 CODEARTS_THINKING_AS_TEXT</label>
          <select
            id="cfg-thinking-as-text"
            data-slot="select-trigger"
            className={inputCls}
            value={cfg.CODEARTS_THINKING_AS_TEXT}
            onChange={(e) => setCfg({ ...cfg, CODEARTS_THINKING_AS_TEXT: e.target.value })}
          >
            <option value="1">开启</option>
            <option value="0">关闭</option>
          </select>
          <p className="mt-1 text-[12px] text-muted-foreground">仅对 Claude Code 客户端生效：开启后思考内容以灰色文本内联展示。</p>
        </div>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-require-api-key">API Key 强制鉴权 CODEARTS_REQUIRE_API_KEY</label>
          <select
            id="cfg-require-api-key"
            data-slot="select-trigger"
            className={inputCls}
            value={cfg.CODEARTS_REQUIRE_API_KEY}
            onChange={(e) => setCfg({ ...cfg, CODEARTS_REQUIRE_API_KEY: e.target.value })}
          >
            <option value="1">开启</option>
            <option value="0">关闭</option>
          </select>
          <p className="mt-1 text-[12px] text-muted-foreground">关闭后 /v1/* 不再校验 API Key；未设置 ADMIN_PASSWORD（管理面禁用）时该开关不生效。</p>
        </div>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-account-name">账号显示名 CODEARTS_ACCOUNT_NAME</label>
          <input
            id="cfg-account-name"
            type="text"
            data-slot="input"
            className={inputCls}
            value={cfg.CODEARTS_ACCOUNT_NAME}
            placeholder="留空则显示主账号"
            onChange={(e) => setCfg({ ...cfg, CODEARTS_ACCOUNT_NAME: e.target.value })}
          />
        </div>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-pricing">模型单价表 CODEARTS_PRICING（USD / 1M tokens）</label>
          <textarea
            id="cfg-pricing"
            data-slot="input"
            className={`${inputCls} min-h-[140px] resize-y font-mono`}
            value={cfg.CODEARTS_PRICING}
            placeholder={'{\n  "model-id": {"input": 0.1, "output": 0.3}\n}'}
            onChange={(e) => setCfg({ ...cfg, CODEARTS_PRICING: e.target.value })}
          />
          <p className="mt-1 text-[12px] text-muted-foreground">键为上游模型 id；input / output 必填，cache_read / cache_creation 缺省按 input 计价。</p>
        </div>
        <div className="mb-4">
          <label className={labelCls} htmlFor="cfg-credits-rate">Credits 换算系数 CODEARTS_CREDITS_RATE</label>
          <input
            id="cfg-credits-rate"
            type="number"
            step="any"
            min="0"
            data-slot="input"
            className={inputCls}
            value={cfg.CODEARTS_CREDITS_RATE}
            placeholder="1.0"
            onChange={(e) => setCfg({ ...cfg, CODEARTS_CREDITS_RATE: e.target.value })}
          />
          <p className="mt-1 text-[12px] text-muted-foreground">Credits = 费用 × 系数，默认 1.0。</p>
        </div>
        <button
          type="button"
          onClick={() => void saveCfg()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          保存
        </button>
        {cfgMsg && <p className="mt-2 text-[12px] text-muted-foreground">{cfgMsg}</p>}
      </div>

      <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
        <label className={labelCls} htmlFor="cfg-snap-session">CODEARTS_SNAP_SESSION</label>
        <input
          id="cfg-snap-session"
          type="password"
          data-slot="input"
          className={inputCls}
          value={snap}
          placeholder={data.secrets?.CODEARTS_SNAP_SESSION ? '已配置（粘贴新值覆盖）；掩码或纯 * 串不覆盖' : '粘贴新值；掩码或纯 * 串不覆盖'}
          onChange={(e) => setSnap(e.target.value)}
        />
        <button
          type="button"
          onClick={() => void saveSnap()}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90"
        >
          保存 session
        </button>
        {snapMsg && <p className="mt-2 text-[12px] text-muted-foreground">{snapMsg}</p>}
      </div>
    </div>
  )
}
