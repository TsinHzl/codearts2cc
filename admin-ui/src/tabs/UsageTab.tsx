import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { api } from '../api'

interface Summary {
  requests?: number
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  cache_creation_tokens?: number
}

interface GroupStat {
  requests: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens?: number
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
  cache_creation_tokens?: number
  first_token_ms?: number | null
  duration_ms?: number | null
  account?: string
}

interface PageInfo {
  page: number
  limit: number
  total: number
}

interface PageTotals {
  requests: number
  priced_requests: number
  cost: number | null
  credits: number | null
  saved: number | null
}

interface DailyRow {
  date: string
  requests: number
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_creation_tokens: number
  cost: number | null
  credits: number | null
  saved: number | null
  unpriced_requests: number
}

interface UsageData {
  summary?: Summary
  by_model?: Record<string, GroupStat>
  entries?: UsageEntry[]
  page?: PageInfo
  page_totals?: PageTotals
  daily?: DailyRow[] | null
  account?: string
  start?: string
  end?: string
}

type Preset = 'today' | '7d' | '14d' | '30d' | 'custom'
type FixedPreset = Exclude<Preset, 'custom'>

const PRESETS: Array<{ key: FixedPreset; label: string }> = [
  { key: 'today', label: '今日' },
  { key: '7d', label: '7 天' },
  { key: '14d', label: '14 天' },
  { key: '30d', label: '30 天' },
]

const PRESET_DAYS: Record<FixedPreset, number> = { today: 1, '7d': 7, '14d': 14, '30d': 30 }

const isoDate = (date: Date): string => {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

const presetRange = (preset: FixedPreset): { start: string; end: string } => {
  const end = new Date()
  const start = new Date()
  start.setDate(end.getDate() - (PRESET_DAYS[preset] - 1))
  return { start: isoDate(start), end: isoDate(end) }
}

const fmt = (n?: number | null): string => (n ?? 0).toLocaleString()
const fmtCost = (v?: number | null): string => (v == null ? '–' : `$${v.toFixed(4)}`)
const fmtCredits = (v?: number | null): string => (v == null ? '–' : `${v.toFixed(4)} 积分`)
const fmtMs = (ms?: number | null): string => (ms == null ? '–' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`)
const fmtTs = (ts?: string): string => {
  if (typeof ts !== 'string' || !ts) return '–'
  return ts.replace('T', ' ').slice(0, 19).replace(/-/g, '/')
}

const th = 'px-3 py-2 text-left text-[12px] font-semibold text-muted-foreground whitespace-nowrap'
const td = 'px-3 py-2 text-[13px] tabular-nums whitespace-nowrap'
const pageBtn = 'rounded-lg border border-border bg-card px-3 py-1.5 text-[13px] font-semibold text-foreground transition-colors hover:bg-muted/60 disabled:cursor-not-allowed disabled:opacity-50'
const dateInput = 'rounded-lg border border-input bg-background px-2 py-1 text-[12px] text-foreground outline-none transition-colors focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30'

function CacheRing({ pct }: { pct: number }) {
  const radius = 14
  const circumference = 2 * Math.PI * radius
  const dash = (Math.min(100, Math.max(0, pct)) / 100) * circumference
  return (
    <svg viewBox="0 0 36 36" className="size-10 -rotate-90 text-primary" role="img" aria-label={`缓存读取占比 ${pct.toFixed(1)}%`}>
      <circle cx="18" cy="18" r={radius} fill="none" strokeWidth="4" className="text-border" stroke="currentColor" />
      <circle cx="18" cy="18" r={radius} fill="none" strokeWidth="4" stroke="currentColor" strokeLinecap="round" strokeDasharray={`${dash} ${circumference}`} />
    </svg>
  )
}

function StatTile({ label, value, note, extra }: { label: string; value: string; note: string; extra?: ReactNode }) {
  return (
    <div data-slot="status-tile" className="stat-card-accent rounded-lg border border-border/80 p-3">
      <div className="text-[12px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-xl font-bold tabular-nums text-foreground">{value}</div>
      <div className="mt-0.5 flex items-center justify-between gap-2">
        <div className="text-[12px] text-muted-foreground">{note}</div>
        {extra}
      </div>
    </div>
  )
}

export default function UsageTab() {
  const [data, setData] = useState<UsageData | null>(null)
  const [err, setErr] = useState('')
  const [preset, setPreset] = useState<Preset>('7d')
  const [range, setRange] = useState(() => presetRange('7d'))
  const [daily, setDaily] = useState(false)
  const [page, setPage] = useState(1)
  const [groupsOpen, setGroupsOpen] = useState(false)
  const [tick, setTick] = useState(0)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let ignore = false
    const params = new URLSearchParams({ page: String(page), limit: '100' })
    if (range.start) params.set('start', range.start)
    if (range.end) params.set('end', range.end)
    if (daily) params.set('daily', 'true')
    setErr('')
    setLoading(true)
    api<UsageData>(`/api/admin/usage?${params.toString()}`)
      .then((next) => { if (!ignore) setData(next) })
      .catch((e) => { if (!ignore) setErr(e.message) })
      .finally(() => { if (!ignore) setLoading(false) })
    return () => { ignore = true }
  }, [page, range.start, range.end, daily, tick])

  const applyPreset = (next: FixedPreset) => {
    setPreset(next)
    setRange(presetRange(next))
    setPage(1)
  }

  const changeRange = (key: 'start' | 'end', value: string) => {
    if (!value) return
    setPreset('custom')
    setRange((prev) => {
      const next = { ...prev, [key]: value }
      return next.start > next.end ? { start: next.end, end: next.start } : next
    })
    setPage(1)
  }

  const toggleDaily = () => {
    setDaily((prev) => !prev)
    setPage(1)
  }

  const entries = data?.entries || []
  const info = data?.page
  const totals = data?.page_totals
  const total = info?.total ?? 0
  const curPage = info?.page ?? 1
  const limit = info?.limit ?? 100
  const pages = Math.max(1, Math.ceil(total / limit))
  const pageTokens = entries.reduce(
    (sum, e) => sum + (e.input_tokens || 0) + (e.output_tokens || 0) + (e.cache_read_tokens || 0) + (e.cache_creation_tokens || 0),
    0,
  )
  const pageCacheRead = entries.reduce((sum, e) => sum + (e.cache_read_tokens || 0), 0)
  const pageInputTotal = entries.reduce((sum, e) => sum + (e.input_tokens || 0) + (e.cache_read_tokens || 0), 0)
  const cachePct = pageInputTotal > 0 ? (pageCacheRead / pageInputTotal) * 100 : 0
  const pricedCount = totals?.priced_requests ?? 0
  const avgCost = pricedCount > 0 && totals?.cost != null ? totals.cost / pricedCount : null
  const unpriced = Math.max(0, (totals?.requests ?? entries.length) - pricedCount)
  const costNote = [
    avgCost != null ? `单请求均价 $${avgCost.toFixed(4)}` : '',
    unpriced > 0 ? `另有 ${unpriced} 条未计价` : '',
  ].filter(Boolean).join(' · ') || '暂无记录'
  const savedNote = totals?.saved != null && totals.saved > 0
    ? `本页缓存节省额 ${fmtCost(totals.saved)}`
    : '本页无缓存节省额'
  const groups = Object.entries(data?.by_model || {})
  const dailyRows = data?.daily || []
  const accountName = data?.account || '主账号'

  return (
    <div className="flex flex-col gap-4">
      <div data-slot="page-header">
        <nav className="text-[12px] text-muted-foreground" aria-label="面包屑">
          <span>主要</span>
          <span className="mx-1.5">/</span>
          <span className="text-foreground">使用日志</span>
        </nav>
        <h2 className="mt-1 text-xl font-bold text-foreground">使用日志</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">按时间维度查看用量与请求明细</p>
      </div>

      <div className="toolbar-surface flex flex-wrap items-center gap-2">
        <div className="inline-flex items-center gap-0.5 rounded-lg border border-border bg-background p-0.5" role="group" aria-label="时间范围预设">
          {PRESETS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => applyPreset(item.key)}
              aria-pressed={preset === item.key}
              className={`rounded-md px-2.5 py-1 text-[12px] font-semibold transition-colors ${preset === item.key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted/60 hover:text-foreground'}`}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="inline-flex items-center gap-1.5">
          <input
            type="date"
            data-slot="input"
            className={dateInput}
            value={range.start}
            onChange={(e) => changeRange('start', e.target.value)}
            aria-label="开始日期"
          />
          <span>至</span>
          <input
            type="date"
            data-slot="input"
            className={dateInput}
            value={range.end}
            onChange={(e) => changeRange('end', e.target.value)}
            aria-label="结束日期"
          />
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            onClick={toggleDaily}
            aria-pressed={daily}
            className={`rounded-lg border px-2.5 py-1 text-[12px] font-semibold transition-colors ${daily ? 'border-primary/30 bg-primary/10 text-primary' : 'border-border bg-card text-muted-foreground hover:bg-muted/60 hover:text-foreground'}`}
          >
            每日统计
          </button>
          <button type="button" onClick={() => setTick(tick + 1)} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground" title="刷新">
            <RefreshCw className="size-3.5" /> 刷新
          </button>
        </div>
      </div>

      {err && (
        <div className="rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
      )}

      {!data ? (
        <p className="text-[13px] text-muted-foreground">{err ? '加载失败，请重试' : '加载中…'}</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatTile
              label="总请求数"
              value={fmt(total)}
              note={`当页 ${entries.length} / 共 ${total} 条 | 第 ${curPage} / ${pages} 页`}
            />
            <StatTile label="本页费用" value={fmtCost(totals?.cost)} note={costNote} />
            <StatTile label="本页 Credits" value={fmtCredits(totals?.credits)} note={savedNote} />
            <StatTile
              label="本页 Token"
              value={fmt(pageTokens)}
              note={`缓存读取 ${fmt(pageCacheRead)} ${cachePct.toFixed(1)}%`}
              extra={<CacheRing pct={cachePct} />}
            />
          </div>

          {groups.length > 0 && (
            <div className="rounded-xl border border-border bg-card shadow-sm" data-slot="card">
              <button
                type="button"
                onClick={() => setGroupsOpen((open) => !open)}
                aria-expanded={groupsOpen}
                className="flex w-full items-center justify-between px-4 py-2.5 text-left"
              >
                <span className="text-[14px] font-bold text-foreground">按模型分组</span>
                {groupsOpen ? <ChevronDown className="size-4 text-muted-foreground" /> : <ChevronRight className="size-4 text-muted-foreground" />}
              </button>
              {groupsOpen && (
                <div className="border-t border-border">
                  <div className="data-table-shell rounded-none border-0">
                    <table className="w-full">
                      <thead>
                        <tr data-slot="table-head">
                          <th className={th}>模型</th><th className={th}>请求数</th><th className={th}>输入</th><th className={th}>输出</th><th className={th}>缓存读取</th><th className={th}>缓存写入</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border/60">
                        {groups.map(([model, group]) => (
                          <tr key={model} data-slot="table-row">
                            <td className="px-3 py-2 font-mono text-[13px]">{model}</td>
                            <td className={td}>{fmt(group.requests)}</td>
                            <td className={td}>{fmt(group.input_tokens)}</td>
                            <td className={td}>{fmt(group.output_tokens)}</td>
                            <td className={td}>{fmt(group.cache_read_tokens)}</td>
                            <td className={td}>{fmt(group.cache_creation_tokens)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="rounded-xl border border-border bg-card shadow-sm" data-slot="card">
            <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
              <h2 className="text-[14px] font-bold text-foreground">{daily ? '每日统计' : '请求日志'}</h2>
              <span className="text-[12px] text-muted-foreground">{daily ? '按日聚合' : `${total} 条，第 ${curPage}/${pages} 页`}</span>
            </div>
            <div className="data-table-shell rounded-none border-0">
              <div data-slot="table-container">
                {daily ? (
                  <table className="usage-logs-table w-full">
                    <thead>
                      <tr data-slot="table-head">
                        <th className={th}>日期</th><th className={th}>请求数</th><th className={th}>输入</th><th className={th}>输出</th><th className={th}>缓存读取</th><th className={th}>缓存写入</th><th className={th}>费用</th><th className={th}>Credits</th><th className={th}>节省</th><th className={th}>未计价条数</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {dailyRows.map((row) => (
                        <tr key={row.date} data-slot="table-row">
                          <td className="px-3 py-2 text-[13px] whitespace-nowrap">{row.date}</td>
                          <td className={td}>{fmt(row.requests)}</td>
                          <td className={td}>{fmt(row.input_tokens)}</td>
                          <td className={td}>{fmt(row.output_tokens)}</td>
                          <td className={td}>{fmt(row.cache_read_tokens)}</td>
                          <td className={td}>{fmt(row.cache_creation_tokens)}</td>
                          <td className={td}>{fmtCost(row.cost)}</td>
                          <td className={td}>{fmtCredits(row.credits)}</td>
                          <td className={td}>{fmtCost(row.saved)}</td>
                          <td className={td}>{fmt(row.unpriced_requests)}</td>
                        </tr>
                      ))}
                      {dailyRows.length === 0 && !loading && (
                        <tr><td colSpan={10} className="px-3 py-4 text-center text-[13px] text-muted-foreground">该区间无记录</td></tr>
                      )}
                    </tbody>
                  </table>
                ) : (
                  <table className="usage-logs-table w-full">
                    <thead>
                      <tr data-slot="table-head">
                        <th className={th}>时间</th><th className={th}>IP</th><th className={th}>账号</th><th className={th}>模型</th><th className={th}>TOKEN 用量</th><th className={th}>用时</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {entries.map((e, index) => {
                        const inputTotal = (e.input_tokens || 0) + (e.cache_read_tokens || 0)
                        return (
                          <tr key={index} data-slot="table-row">
                            <td className="px-3 py-2 text-[13px] whitespace-nowrap">{fmtTs(e.ts)}</td>
                            <td className="px-3 py-2 font-mono text-[13px] whitespace-nowrap">{e.ip || '–'}</td>
                            <td className="px-3 py-2 text-[13px] whitespace-nowrap">{e.account || accountName}</td>
                            <td className="px-3 py-2 text-[13px] whitespace-nowrap">
                              <div className="font-mono">{typeof e.model === 'string' && e.model ? e.model : '–'}</div>
                              <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
                                {e.endpoint && <span>{e.endpoint}</span>}
                                <span>{e.stream ? '流式' : '非流式'}</span>
                                {e.effort && <span>{e.effort}</span>}
                              </div>
                            </td>
                            <td className="px-3 py-2 text-[12px] leading-5 whitespace-nowrap">
                              <div>输入 Tokens: <span className="tabular-nums">{fmt(e.input_tokens)}</span></div>
                              <div>输出 Tokens: <span className="tabular-nums">{fmt(e.output_tokens)}</span></div>
                              <div>缓存读取: <span className="tabular-nums">{fmt(e.cache_read_tokens)}</span></div>
                              <div>缓存写入: <span className="tabular-nums">{fmt(e.cache_creation_tokens)}</span></div>
                              <div className="font-semibold">输入总计: <span className="tabular-nums">{fmt(inputTotal)}</span></div>
                            </td>
                            <td className={td} title={e.first_token_ms == null ? undefined : `首字耗时 ${fmtMs(e.first_token_ms)}`}>
                              {fmtMs(e.duration_ms)}
                            </td>
                          </tr>
                        )
                      })}
                      {entries.length === 0 && !loading && (
                        <tr><td colSpan={6} className="px-3 py-4 text-center text-[13px] text-muted-foreground">暂无记录</td></tr>
                      )}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
            {!daily && (
              <div className="flex items-center gap-2 border-t border-border px-4 py-3">
                <button type="button" disabled={curPage <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))} className={pageBtn}>
                  上一页
                </button>
                <button type="button" disabled={curPage >= pages} onClick={() => setPage((p) => p + 1)} className={pageBtn}>
                  下一页
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
