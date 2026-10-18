import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Loader2, LogIn, LogOut } from 'lucide-react'
import { api } from '../api'
import OAuthLoginModal from '@/components/OAuthLoginModal'

interface OAuthOverview {
  logged_in?: boolean
  needs_relogin?: boolean
  user_name?: string
  expiration?: string
}

interface OverviewData {
  listen?: string
  upstream?: string
  model_count?: number
  agent_count?: number
  benefit_count?: number
  cache_sim_enabled?: boolean
  public_exposed?: boolean
  oauth?: OAuthOverview
}

const primaryBtn = 'inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-[12px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60'
const dangerBtn = 'inline-flex items-center gap-1.5 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-1.5 text-[12px] font-semibold text-destructive transition-colors hover:bg-destructive/15 disabled:cursor-not-allowed disabled:opacity-60'

export default function OverviewTab() {
  const [data, setData] = useState<OverviewData | null>(null)
  const [err, setErr] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)

  const load = useCallback(() => {
    api<OverviewData>('/api/admin/overview').then(setData).catch((e) => setErr(e.message))
  }, [])

  useEffect(() => { load() }, [load])

  const logout = () => {
    if (loggingOut) return
    setLoggingOut(true)
    api('/api/admin/oauth/logout', { method: 'POST' })
      .then(() => { load() })
      .catch((e) => setErr(e.message))
      .finally(() => setLoggingOut(false))
  }

  if (err) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  const oauth = data.oauth
  const loggedIn = Boolean(oauth?.logged_in)
  const needsRelogin = Boolean(oauth?.needs_relogin)
  const oauthText = loggedIn
    ? needsRelogin ? '需重新登录' : `已登录${oauth?.user_name ? `（${oauth.user_name}）` : ''}`
    : '未登录'

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
          <tr className="border-b border-border/60"><th scope="row" className="py-2 pr-4 text-left font-medium text-muted-foreground">监听</th><td className="py-2 font-mono text-foreground">{data.listen}</td></tr>
          <tr className="border-b border-border/60"><th scope="row" className="py-2 pr-4 text-left font-medium text-muted-foreground">上游</th><td className="py-2 font-mono break-all text-foreground">{data.upstream}</td></tr>
          <tr className="border-b border-border/60">
            <th scope="row" className="py-2 pr-4 text-left font-medium text-muted-foreground">CodeArts 登录</th>
            <td className="py-2 text-foreground">
              <div className="flex flex-wrap items-center gap-2">
                <span>{oauthText}</span>
                {loggedIn && !needsRelogin ? (
                  <button type="button" onClick={logout} disabled={loggingOut} className={dangerBtn}>
                    {loggingOut ? <Loader2 className="size-3.5 animate-spin" /> : <LogOut className="size-3.5" />}
                    退出登录
                  </button>
                ) : (
                  <button type="button" onClick={() => setModalOpen(true)} className={primaryBtn}>
                    <LogIn className="size-3.5" />
                    登录
                  </button>
                )}
              </div>
            </td>
          </tr>
          <tr className="border-b border-border/60"><th scope="row" className="py-2 pr-4 text-left font-medium text-muted-foreground">模型数</th><td className="py-2 text-foreground">{data.model_count}（agent {data.agent_count} / benefit {data.benefit_count}）</td></tr>
          <tr><th scope="row" className="py-2 pr-4 text-left font-medium text-muted-foreground">缓存模拟</th><td className="py-2 text-foreground">{data.cache_sim_enabled ? '开启' : '关闭'}</td></tr>
        </tbody>
      </table>
      <OAuthLoginModal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        onSettled={(result) => {
          if (result === 'ok') { setModalOpen(false); load() }
        }}
      />
    </div>
  )
}
