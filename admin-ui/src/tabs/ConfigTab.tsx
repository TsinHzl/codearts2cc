import { useEffect, useState } from 'react'
import { api } from '../api'

interface ConfigData {
  editable?: Record<string, string>
  secrets?: Record<string, string | undefined>
}

const inputCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30'
const labelCls = 'mb-1.5 block text-[12px] font-semibold text-muted-foreground'

export default function ConfigTab() {
  const [data, setData] = useState<ConfigData | null>(null)
  const [err, setErr] = useState('')
  const [cfgMsg, setCfgMsg] = useState('')
  const [snapMsg, setSnapMsg] = useState('')
  const [cfg, setCfg] = useState({ CODEARTS_ANTHROPIC_MODEL_MAP: '', CODEARTS_CACHE_SIM: '1' })
  const [snap, setSnap] = useState('')

  const load = () =>
    api<ConfigData>('/api/admin/config').then((d) => {
      setData(d)
      setCfg({
        CODEARTS_ANTHROPIC_MODEL_MAP: d.editable?.CODEARTS_ANTHROPIC_MODEL_MAP || '',
        CODEARTS_CACHE_SIM: d.editable?.CODEARTS_CACHE_SIM !== '0' ? '1' : '0',
      })
      setSnap('') // 掩码不载入输入框，仅作 placeholder 提示；提交掩码后端也会跳过
    }).catch((e) => setErr(e.message))

  useEffect(() => { void load() }, [])

  if (err && !data) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  const saveCfg = () =>
    api('/api/admin/config', {
      method: 'PUT',
      body: JSON.stringify({ editable: cfg }),
    })
      .then(() => setCfgMsg('已保存（立即生效）'))
      .catch((e) => setCfgMsg(e.message))

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
        <h3 className="mb-3 text-[14px] font-bold text-foreground">密钥（只读脱敏）</h3>
        <table className="w-full text-[13px]">
          <tbody>
            <tr className="border-b border-border/60"><th className="py-2 pr-4 text-left font-medium text-muted-foreground">CODEARTS_AK</th><td className="py-2 font-mono break-all text-foreground">{data.secrets?.CODEARTS_AK || '(未配置)'}</td></tr>
            <tr><th className="py-2 pr-4 text-left font-medium text-muted-foreground">CODEARTS_SK</th><td className="py-2 font-mono break-all text-foreground">{data.secrets?.CODEARTS_SK || '(未配置)'}</td></tr>
          </tbody>
        </table>
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
