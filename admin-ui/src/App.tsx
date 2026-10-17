import { useCallback, useEffect, useState } from 'react'
import { Boxes, Loader2 } from 'lucide-react'
import { api } from './api'
import Layout, { type TabKey } from '@/components/Layout'
import OverviewTab from './tabs/OverviewTab'
import ModelsTab from './tabs/ModelsTab'
import BenefitTab from './tabs/BenefitTab'
import UsageTab from './tabs/UsageTab'
import ConfigTab from './tabs/ConfigTab'

export default function App() {
  const [authed, setAuthed] = useState(false)
  const [tab, setTab] = useState<TabKey>('overview')
  const [loginErr, setLoginErr] = useState('')
  const [pw, setPw] = useState('')
  const [loggingIn, setLoggingIn] = useState(false)

  // 登录态探测：与原 checkAuth 语义一致
  const checkAuth = useCallback(() =>
    api('/api/admin/overview')
      .then(() => setAuthed(true))
      .catch(() => setAuthed(false)), [])

  useEffect(() => { void checkAuth() }, [checkAuth])

  const login = async () => {
    if (!pw || loggingIn) return
    setLoggingIn(true)
    try {
      await api('/api/admin/login', { method: 'POST', body: JSON.stringify({ password: pw }) })
      setLoginErr('')
      await checkAuth()
    } catch (e) {
      setLoginErr(e instanceof Error ? e.message : String(e))
    } finally {
      setLoggingIn(false)
    }
  }

  const logout = () =>
    api('/api/admin/logout', { method: 'POST' }).finally(() => setAuthed(false))

  if (!authed) {
    return (
      <main className="flex min-h-dvh items-center justify-center p-4">
        <div className="w-full max-w-sm rounded-xl border border-border bg-card p-6 shadow-sm" data-slot="card">
          <div className="mb-5 flex flex-col items-center gap-2 text-center">
            <div className="flex size-12 items-center justify-center rounded-xl bg-primary/15">
              <Boxes className="size-6 text-primary" />
            </div>
            <h2 className="text-lg font-bold text-foreground">CodeArts 代理管理</h2>
            <p className="text-[13px] text-muted-foreground">输入管理口令以继续</p>
          </div>
          <input
            type="password"
            placeholder="管理口令"
            aria-label="管理口令"
            value={pw}
            onChange={(e) => setPw(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void login() }}
            className="w-full rounded-lg border border-input bg-background px-3 py-2 text-[14px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30"
          />
          <button
            type="button"
            onClick={() => void login()}
            disabled={!pw || loggingIn}
            className="mt-3 inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[14px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {loggingIn && <Loader2 className="size-4 animate-spin" />}
            登录
          </button>
          {loginErr && (
            <div className="mt-3 rounded-md border border-destructive/25 bg-destructive/10 px-2.5 py-2 text-[12px] font-medium leading-relaxed text-destructive" role="alert">
              {loginErr}
            </div>
          )}
        </div>
      </main>
    )
  }

  return (
    <Layout active={tab} onNavigate={setTab} onLogout={logout}>
      {tab === 'overview' && <OverviewTab />}
      {tab === 'models' && <ModelsTab />}
      {tab === 'benefit' && <BenefitTab />}
      {tab === 'usage' && <UsageTab />}
      {tab === 'config' && <ConfigTab />}
    </Layout>
  )
}
