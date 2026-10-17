import { useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../api'

interface Summary {
  requests?: number
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
}

interface GroupStat {
  requests: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
}

interface UsageEntry {
  ts?: string
  ip?: string
  endpoint?: string
  model?: string
  stream?: boolean
  effort?: string
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  first_token_ms?: number | null
  duration_ms?: number | null
}

interface UsageData {
  summary?: Summary
  by_model?: Record<string, GroupStat>
  entries?: UsageEntry[]
  total?: number
  page?: number
  limit?: number
}

const fmt = (n?: number | null) => (n ?? 0).toLocaleString()
const fmtMs = (ms?: number | null) => (ms == null ? '-' : ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms')
const fmtTs = (ts?: string) => String(ts || '').replace('T', ' ')

const th = 'px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground whitespace-nowrap'
const td = 'px-3 py-2 text-[13px] tabular-nums whitespace-nowrap'

const badgeCls = 'inline-flex rounded-md border px-1.5 py-0.5 text-[11px] font-bold'

function EndpointBadge({ endpoint }: { endpoint?: string }) {
  if (!endpoint) return <>-</>
  const isMessages = endpoint.includes('messages')
  return (
    <span className={`${badgeCls} ${isMessages ? 'border-info/25 bg-info/10 text-info' : 'border-primary/20 bg-primary/10 text-primary'}`}>{endpoint}</span>
  )
}

export default function UsageTab() {
  const [data, setData] = useState<UsageData | null>(null)
  const [err, setErr] = useState('')
  const [page, setPage] = useState(1)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    api<UsageData>(`/api/admin/usage?page=${page}&limit=50`).then(setData).catch((e) => setErr(e.message))
  }, [page, tick])

  if (err && !data) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  const s = data.summary || {}
  const groups = Object.entries(data.by_model || {})
  const total = data.total || 0
  const curPage = data.page ?? 1
  const pages = Math.max(1, Math.ceil(total / (data.limit || 50)))

  const statItems: Array<{ label: string; value: number | undefined }> = [
    { label: '总请求数', value: s.requests },
    { label: '输入 Tokens', value: s.input_tokens },
    { label: '输出 Tokens', value: s.output_tokens },
    { label: '缓存读取 Tokens', value: s.cache_read_tokens },
  ]

  return (
    <div className="flex flex-col gap-4">
      {err && (
        <div className="rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
      )}

      <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-bold text-foreground">使用统计</h2>
          <button type="button" onClick={() => setTick(tick + 1)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground" title="刷新">
            <RefreshCw className="size-3.5" /> 刷新
          </button>
        </div>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {statItems.map((item) => (
            <div key={item.label} data-slot="status-tile" className="stat-card-accent rounded-lg border border-border/80 p-3">
              <div className="text-xl font-bold tabular-nums text-foreground">{fmt(item.value)}</div>
              <div className="mt-0.5 text-[12px] text-muted-foreground">{item.label}</div>
            </div>
          ))}
        </div>
      </div>

      {groups.length > 0 && (
        <div className="rounded-xl border border-border bg-card shadow-sm" data-slot="card">
          <div className="border-b border-border px-4 py-2.5">
            <h2 className="text-[14px] font-bold text-foreground">按模型分组</h2>
          </div>
          <div className="data-table-shell rounded-none border-0">
            <table className="w-full">
              <thead>
                <tr data-slot="table-head">
                  <th className={th}>模型</th><th className={th}>请求数</th><th className={th}>输入</th><th className={th}>输出</th><th className={th}>缓存读取</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {groups.map(([mo, g]) => (
                  <tr key={mo} data-slot="table-row">
                    <td className="px-3 py-2 font-mono text-[13px]">{mo}</td>
                    <td className={td}>{fmt(g.requests)}</td>
                    <td className={td}>{fmt(g.input_tokens)}</td>
                    <td className={td}>{fmt(g.output_tokens)}</td>
                    <td className={td}>{fmt(g.cache_read_tokens)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="rounded-xl border border-border bg-card shadow-sm" data-slot="card">
        <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
          <h2 className="text-[14px] font-bold text-foreground">请求明细</h2>
          <span className="text-[12px] text-muted-foreground">{total} 条，第 {curPage}/{pages} 页</span>
        </div>
        <div className="data-table-shell rounded-none border-0">
          <div data-slot="table-container">
            <table className="usage-logs-table w-full">
              <thead>
                <tr data-slot="table-head">
                  <th className={th}>时间</th><th className={th}>IP</th><th className={th}>端点</th><th className={th}>模型</th><th className={th}>标签</th><th className={th}>输入</th><th className={th}>输出</th><th className={th}>缓存读取</th><th className={th}>输入总计</th><th className={th}>首字</th><th className={th}>总耗时</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/60">
                {(data.entries || []).map((e, i) => {
                  const totalIn = (e.input_tokens || 0) + (e.cache_read_tokens || 0)
                  return (
                    <tr key={i} data-slot="table-row">
                      <td className="px-3 py-2 text-[13px] whitespace-nowrap">{fmtTs(e.ts)}</td>
                      <td className={td}>{e.ip || '-'}</td>
                      <td className="px-3 py-2"><EndpointBadge endpoint={e.endpoint} /></td>
                      <td className="px-3 py-2 font-mono text-[13px]">{e.model}</td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        <span className={`${badgeCls} ${e.stream ? 'border-info/25 bg-info/10 text-info' : 'border-border bg-muted/60 text-muted-foreground'}`}>{e.stream ? '流式' : '非流式'}</span>
                        {e.effort && <span> <span className={`${badgeCls} border-border bg-muted/60 text-muted-foreground`}>{e.effort}</span></span>}
                      </td>
                      <td data-slot="table-cell" className={td}>{fmt(e.input_tokens)}</td>
                      <td data-slot="table-cell" className={td}>{fmt(e.output_tokens)}</td>
                      <td data-slot="table-cell" className={td}>{fmt(e.cache_read_tokens)}</td>
                      <td data-slot="table-cell" className={td}>{fmt(totalIn)}</td>
                      <td data-slot="table-cell" className={td}>{fmtMs(e.first_token_ms)}</td>
                      <td data-slot="table-cell" className={td}>{fmtMs(e.duration_ms)}</td>
                    </tr>
                  )
                })}
                {(!data.entries || data.entries.length === 0) && (
                  <tr><td colSpan={11} className="px-3 py-4 text-center text-[13px] text-muted-foreground">暂无记录</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <button
            type="button"
            disabled={curPage <= 1}
            onClick={() => setPage(curPage - 1)}
            className="rounded-lg border border-border bg-card px-3 py-1.5 text-[13px] font-semibold text-foreground transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-50"
          >
            上一页
          </button>
          <button
            type="button"
            disabled={curPage >= pages}
            onClick={() => setPage(curPage + 1)}
            className="rounded-lg border border-border bg-card px-3 py-1.5 text-[13px] font-semibold text-foreground transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-50"
          >
            下一页
          </button>
        </div>
      </div>
    </div>
  )
}
