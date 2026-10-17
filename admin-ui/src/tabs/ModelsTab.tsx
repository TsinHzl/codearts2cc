import { useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api'

interface ModelItem {
  id: string
  name: string
  channel: string
  context_window?: number | null
  max_tokens?: number | null
  supports_images?: boolean
}

interface ModelsData {
  agent: ModelItem[]
  benefit: ModelItem[]
}

function ModelRows({ models }: { models: ModelItem[] }) {
  return (
    <>
      {models.map((x) => (
        <tr key={x.id} data-slot="table-row">
          <td className="px-3 py-2 font-mono text-[13px]">{x.id}</td>
          <td className="px-3 py-2 text-[13px]">{x.name}</td>
          <td className="px-3 py-2">
            <span className="inline-flex rounded-md border border-primary/20 bg-primary/10 px-1.5 py-0.5 text-[11px] font-bold text-primary">{x.channel}</span>
          </td>
          <td data-slot="table-cell" className="px-3 py-2 text-[13px] tabular-nums">{x.context_window ?? '-'}</td>
          <td data-slot="table-cell" className="px-3 py-2 text-[13px] tabular-nums">{x.max_tokens ?? '-'}</td>
          <td data-slot="table-cell" className="px-3 py-2 text-[13px]">{x.supports_images ? '是' : '否'}</td>
        </tr>
      ))}
    </>
  )
}

function ModelTable({ title, models }: { title: string; models: ModelItem[] }) {
  return (
    <div className="rounded-xl border border-border bg-card shadow-sm" data-slot="card">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <h2 className="text-[14px] font-bold text-foreground">{title}</h2>
        <span className="text-[12px] text-muted-foreground">{models.length} 个</span>
      </div>
      <div className="data-table-shell rounded-none border-0">
        <table className="w-full">
          <thead>
            <tr data-slot="table-head">
              <th className="px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground">ID</th>
              <th className="px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground">名称</th>
              <th className="px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground">通道</th>
              <th className="px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground">上下文</th>
              <th className="px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground">最大输出</th>
              <th className="px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground">图片</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/60">
            <ModelRows models={models} />
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default function ModelsTab() {
  const [data, setData] = useState<ModelsData | null>(null)
  const [err, setErr] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  const load = () =>
    api<ModelsData>('/api/admin/models').then(setData).catch((e) => setErr(e.message))

  useEffect(() => { void load() }, [])

  const refresh = () => {
    if (refreshing) return
    setRefreshing(true)
    api('/api/admin/models/refresh', { method: 'POST' })
      .then(load)
      .catch((e) => setErr(e.message))
      .finally(() => setRefreshing(false))
  }

  if (err && !data) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  return (
    <div className="flex flex-col gap-4">
      {err && (
        <div className="rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
      )}
      <ModelTable title="agent（老套餐）" models={data.agent} />
      <ModelTable title="benefit（免费额度）" models={data.benefit} />
      <div>
        <button
          type="button"
          onClick={refresh}
          disabled={refreshing}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {refreshing ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          强制刷新
        </button>
      </div>
    </div>
  )
}
