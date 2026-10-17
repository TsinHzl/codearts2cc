import { useEffect, useState } from 'react'
import { AlertTriangle } from 'lucide-react'
import { api } from '../api'

interface OverviewData {
  listen?: string
  upstream?: string
  model_count?: number
  agent_count?: number
  benefit_count?: number
  cache_sim_enabled?: boolean
  public_exposed?: boolean
}

export default function OverviewTab() {
  const [data, setData] = useState<OverviewData | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    api<OverviewData>('/api/admin/overview').then(setData).catch((e) => setErr(e.message))
  }, [])

  if (err) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  return (
    <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
      <h2 className="mb-3 text-base font-bold text-foreground">概览</h2>
      {data.public_exposed && (
        <div className="mb-3 flex items-center gap-2 rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] font-medium text-destructive" role="alert">
          <AlertTriangle className="size-4 shrink-0" />
          监听地址非回环，管理面已暴露公网
        </div>
      )}
      <table className="w-full text-[13px]">
        <tbody>
          <tr className="border-b border-border/60"><th className="py-2 pr-4 text-left font-medium text-muted-foreground">监听</th><td className="py-2 font-mono text-foreground">{data.listen}</td></tr>
          <tr className="border-b border-border/60"><th className="py-2 pr-4 text-left font-medium text-muted-foreground">上游</th><td className="py-2 font-mono break-all text-foreground">{data.upstream}</td></tr>
          <tr className="border-b border-border/60"><th className="py-2 pr-4 text-left font-medium text-muted-foreground">模型数</th><td className="py-2 text-foreground">{data.model_count}（agent {data.agent_count} / benefit {data.benefit_count}）</td></tr>
          <tr><th className="py-2 pr-4 text-left font-medium text-muted-foreground">缓存模拟</th><td className="py-2 text-foreground">{data.cache_sim_enabled ? '开启' : '关闭'}</td></tr>
        </tbody>
      </table>
    </div>
  )
}
