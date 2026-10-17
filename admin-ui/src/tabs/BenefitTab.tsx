import { useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api'

interface CreditData {
  amount?: number | string
  used?: number | string
  remain?: number | string
}

interface BenefitData {
  total_quota?: number | string
  total_balance?: number | string
  used_amount?: number | string
}

const cellTh = 'py-2 pr-4 text-left font-medium text-muted-foreground align-top'
const cellTd = 'py-2 font-mono break-all text-foreground'

function CreditCard() {
  const [data, setData] = useState<CreditData | null>(null)
  const [err, setErr] = useState('')

  const load = () => {
    setData(null); setErr('')
    api<CreditData>('/api/admin/credit').then(setData).catch((e) => setErr(e.message))
  }
  useEffect(() => { void load() }, [])

  let body
  if (err) {
    body = <p className="text-[13px] text-muted-foreground">{err}</p>
  } else if (!data) {
    body = <p className="text-[13px] text-muted-foreground">加载中…</p>
  } else {
    const a = data.amount, u = data.used
    const ok = typeof a === 'number' && a > 0 && typeof u === 'number'
    const pct = ok ? Math.min(100, Math.max(0, (u / a) * 100)) : null
    body = (
      <>
        <table className="w-full text-[13px]">
          <tbody>
            <tr className="border-b border-border/60"><th className={cellTh}>总量</th><td className={cellTd}>{String(data.amount)}</td></tr>
            <tr className="border-b border-border/60"><th className={cellTh}>已用</th><td className={cellTd}>{String(data.used)}</td></tr>
            <tr><th className={cellTh}>剩余</th><td className={cellTd}>{String(data.remain)}</td></tr>
          </tbody>
        </table>
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
          <i className="block h-full rounded-full bg-primary transition-[width] duration-300" style={{ width: pct == null ? '0%' : pct.toFixed(1) + '%' }} />
        </div>
        <p className="mt-1.5 text-[12px] text-muted-foreground">{pct == null ? '--' : `已用 ${pct.toFixed(1)}%`}</p>
      </>
    )
  }
  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-bold text-foreground">积分用量</h2>
        <button type="button" onClick={load} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground" title="刷新">
          <RefreshCw className="size-3.5" /> 刷新
        </button>
      </div>
      {body}
    </div>
  )
}

export default function BenefitTab() {
  const [data, setData] = useState<BenefitData | null>(null)
  const [err, setErr] = useState('')
  const [claimMsg, setClaimMsg] = useState('')
  const [claiming, setClaiming] = useState(false)

  const load = () =>
    api<BenefitData>('/api/admin/benefit').then(setData).catch((e) => setErr(e.message))

  useEffect(() => { void load() }, [])

  const claim = () => {
    if (claiming) return
    setClaiming(true)
    api('/api/admin/benefit/claim', { method: 'POST' })
      .then(() => { setClaimMsg('签到成功'); setClaiming(false); load() })
      .catch((e) => { setClaiming(false); setClaimMsg(e.message) })
  }

  return (
    <div className="flex flex-col gap-4">
      {err && (
        <div className="rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
      )}
      <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-bold text-foreground">免费额度</h2>
          <button type="button" onClick={load} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground" title="刷新">
            <RefreshCw className="size-3.5" /> 刷新
          </button>
        </div>
        <table className="w-full text-[13px]">
          <tbody>
            <tr className="border-b border-border/60"><th className={cellTh}>总配额</th><td className={cellTd}>{String(data?.total_quota ?? '')}</td></tr>
            <tr className="border-b border-border/60"><th className={cellTh}>剩余</th><td className={cellTd}>{String(data?.total_balance ?? '')}</td></tr>
            <tr><th className={cellTh}>已用</th><td className={cellTd}>{String(data?.used_amount ?? '')}</td></tr>
          </tbody>
        </table>
        <button
          type="button"
          onClick={claim}
          disabled={claiming}
          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {claiming && <Loader2 className="size-4 animate-spin" />}
          立即签到
        </button>
        {claimMsg && <p className="mt-2 text-[12px] text-muted-foreground">{claimMsg}</p>}
      </div>
      <CreditCard />
    </div>
  )
}
