import { useCallback, useEffect, useRef, useState } from 'react'
import { ExternalLink, Loader2, LogIn, LogOut, RefreshCw, XCircle } from 'lucide-react'
import { api } from '../api'

interface OAuthStatus {
  logged_in?: boolean
  needs_relogin?: boolean
  user_id?: string
  user_name?: string
  domain_id?: string
  expiration?: string
  has_refresh_token?: boolean
  remaining_seconds?: number | null
}

interface StartData {
  session_id?: string
  auth_url?: string
  callback_hint?: string
}

interface PollData {
  status?: 'pending' | 'ok' | 'err'
  error?: string
  detail?: string
}

const inputCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30'
const labelCls = 'mb-1.5 block text-[12px] font-semibold text-muted-foreground'
const primaryBtn = 'inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60'
const dangerBtn = 'inline-flex items-center gap-1.5 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-[13px] font-semibold text-destructive transition-colors hover:bg-destructive/15 disabled:cursor-not-allowed disabled:opacity-60'
const cellTh = 'py-2 pr-4 text-left font-medium text-muted-foreground align-top'
const cellTd = 'py-2 font-mono break-all text-foreground'
const POLL_INTERVAL_MS = 1500
const MAX_POLL_FAILURES = 3
const MAX_POLL_DURATION_MS = 10 * 60 * 1000

/** 授权链接仅允许 http(s)：进入 state / DOM / 弹窗导航前统一校验，阻断 javascript: 等伪协议。 */
function isAllowedAuthUrl(raw: string | undefined): boolean {
  if (!raw) return false
  try {
    return new URL(raw).protocol === 'https:' || new URL(raw).protocol === 'http:'
  } catch {
    return false
  }
}

function remainingText(status: OAuthStatus | null): string {
  const seconds = status?.remaining_seconds
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '--'
  if (seconds <= 0) return '已过期'
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return hours > 0 ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function StatusTable({ status, stateText, badgeCls }: { status: OAuthStatus | null; stateText: string; badgeCls: string }) {
  return (
    <table className="w-full text-[13px]">
      <tbody>
        <tr className="border-b border-border/60"><th scope="row" className={cellTh}>状态</th><td className="py-2"><span className={badgeCls}>{stateText}</span></td></tr>
        <tr className="border-b border-border/60"><th scope="row" className={cellTh}>账号</th><td className={cellTd}>{status?.user_name || '--'}</td></tr>
        <tr className="border-b border-border/60"><th scope="row" className={cellTh}>用户 ID</th><td className={cellTd}>{status?.user_id || '--'}</td></tr>
        <tr className="border-b border-border/60"><th scope="row" className={cellTh}>到期时间</th><td className={cellTd}>{status?.expiration || '--'}</td></tr>
        <tr><th scope="row" className={cellTh}>剩余</th><td className={cellTd}>{remainingText(status)}</td></tr>
      </tbody>
    </table>
  )
}

function AuthLinkPanel({ authUrl, callbackHint, pollDetail }: { authUrl: string; callbackHint: string; pollDetail: string }) {
  return (
    <div className="mt-4 rounded-lg border border-border bg-muted/30 p-3">
      <p className="mb-1 text-[12px] font-semibold text-muted-foreground">授权链接</p>
      <a href={authUrl} target="_blank" rel="noreferrer" className="inline-flex items-start gap-1 break-all font-mono text-[12px] text-primary hover:underline">
        {authUrl}
        <ExternalLink className="mt-0.5 size-3.5 shrink-0" />
      </a>
      {callbackHint && (
        <p className="mt-2 text-[12px] text-muted-foreground">浏览器未自动跳转时，请将最终回调地址粘贴到下方导入框（回调前缀：{callbackHint}）。</p>
      )}
      {pollDetail && <p className="mt-1 text-[12px] text-muted-foreground" role="status" aria-live="polite">等待授权…（{pollDetail}）</p>}
    </div>
  )
}

function CallbackImportForm({ sessionId, onSettled }: { sessionId: string; onSettled: (result: 'ok' | 'pending' | 'err', message: string) => void }) {
  const [importUrl, setImportUrl] = useState('')
  const [importing, setImporting] = useState(false)
  const [importMsg, setImportMsg] = useState('')

  const importCallback = () => {
    if (!sessionId || !importUrl.trim() || importing) return
    setImporting(true)
    setImportMsg('')
    api<PollData>('/api/admin/oauth/import-callback', {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId, url: importUrl.trim() }),
    })
      .then((data) => {
        if (data.status === 'ok') {
          setImportUrl('')
          onSettled('ok', '登录成功')
        } else if (data.status === 'err') {
          onSettled('err', data.error || '登录失败')
        } else {
          setImportMsg('已导入，等待授权完成…')
          onSettled('pending', '')
        }
      })
      .catch((e) => onSettled('err', errMsg(e)))
      .finally(() => setImporting(false))
  }

  return (
    <div className="mt-4">
      <label className={labelCls} htmlFor="oauth-callback-url">回调 URL 导入</label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input
          id="oauth-callback-url"
          className={inputCls}
          placeholder="http://127.0.0.1:51818/oauth/callback?..."
          value={importUrl}
          onChange={(e) => setImportUrl(e.target.value)}
        />
        <button type="button" onClick={importCallback} disabled={!importUrl.trim() || importing} className={primaryBtn}>
          {importing && <Loader2 className="size-4 animate-spin" />}
          导入
        </button>
      </div>
      {importMsg && <p className="mt-2 text-[12px] text-muted-foreground" role="status" aria-live="polite">{importMsg}</p>}
    </div>
  )
}

/** 轮询会话 hook：递归 setTimeout + 在途守卫 + 有限失败重试 + 最长时限，统一 resetSession 清理。 */
function useOAuthSession({ onSuccess, onError }: { onSuccess: () => void; onError: (msg: string) => void }) {
  const [sessionId, setSessionId] = useState('')
  const [pollDetail, setPollDetail] = useState('')
  const inFlight = useRef(false)
  const failures = useRef(0)
  const onSuccessRef = useRef(onSuccess)
  const onErrorRef = useRef(onError)
  onSuccessRef.current = onSuccess
  onErrorRef.current = onError

  const resetSession = useCallback(() => {
    setSessionId('')
    setPollDetail('')
    inFlight.current = false
    failures.current = 0
  }, [])

  useEffect(() => {
    if (!sessionId) return
    let stopped = false
    let timer = 0
    const deadline = Date.now() + MAX_POLL_DURATION_MS
    const schedule = () => { timer = window.setTimeout(tick, POLL_INTERVAL_MS) }
    const tick = () => {
      if (stopped) return
      if (inFlight.current) { schedule(); return }
      if (Date.now() >= deadline) {
        stopped = true
        onErrorRef.current('登录超时：请重新发起登录')
        setSessionId('')
        return
      }
      inFlight.current = true
      api<PollData>(`/api/admin/oauth/poll?session_id=${encodeURIComponent(sessionId)}`)
        .then((data) => {
          if (stopped) return
          failures.current = 0
          if (data.status === 'ok') {
            stopped = true
            setSessionId('')
            onSuccessRef.current()
          } else if (data.status === 'err') {
            stopped = true
            setSessionId('')
            onErrorRef.current(data.error || '登录失败')
          } else {
            setPollDetail(data.detail || '')
            schedule()
          }
        })
        .catch((e) => {
          if (stopped) return
          failures.current += 1
          if (failures.current >= MAX_POLL_FAILURES) {
            stopped = true
            setSessionId('')
            onErrorRef.current(errMsg(e))
          } else {
            schedule()
          }
        })
        .finally(() => { inFlight.current = false })
    }
    tick()
    return () => { stopped = true; window.clearTimeout(timer) }
  }, [sessionId])

  const startSession = useCallback((sid: string) => {
    failures.current = 0
    setSessionId(sid)
    setPollDetail('')
  }, [])

  return { sessionId, pollDetail, startSession, resetSession }
}

export default function LoginTab() {
  const [status, setStatus] = useState<OAuthStatus | null>(null)
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')
  const [starting, setStarting] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const [authUrl, setAuthUrl] = useState('')
  const [callbackHint, setCallbackHint] = useState('')

  const loadStatus = useCallback(() => {
    api<OAuthStatus>('/api/admin/oauth/status')
      .then((data) => { setStatus(data); setErr('') })
      .catch((e) => setErr(errMsg(e)))
  }, [])

  useEffect(() => { void loadStatus() }, [loadStatus])

  const session = useOAuthSession({
    onSuccess: () => { setNotice('登录成功'); loadStatus() },
    onError: (msg) => { setErr(msg); setAuthUrl(''); setCallbackHint('') },
  })

  const start = () => {
    if (starting) return
    setStarting(true)
    setErr('')
    setNotice('')
    // 先同步开窗再填地址，避免异步回调触发弹窗拦截
    const popup = window.open('about:blank', '_blank')
    if (popup) popup.opener = null
    api<StartData>('/api/admin/oauth/start', { method: 'POST' })
      .then((data) => {
        const url: string = data.auth_url ?? ''
        if (!data.session_id || !isAllowedAuthUrl(url)) throw new Error('授权链接缺失或非法')
        session.startSession(data.session_id)
        setAuthUrl(url)
        setCallbackHint(data.callback_hint || '')
        if (popup) popup.location.href = url
      })
      .catch((e) => {
        popup?.close()
        setErr(errMsg(e))
      })
      .finally(() => setStarting(false))
  }

  const logout = () => {
    if (loggingOut) return
    setLoggingOut(true)
    setErr('')
    setNotice('')
    api('/api/admin/oauth/logout', { method: 'POST' })
      .then(() => {
        session.resetSession()
        setAuthUrl('')
        setCallbackHint('')
        setNotice('已退出登录')
        loadStatus()
      })
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setLoggingOut(false))
  }

  const cancelLogin = () => {
    session.resetSession()
    setAuthUrl('')
    setCallbackHint('')
    setNotice('已取消登录')
  }

  const loggedIn = Boolean(status?.logged_in)
  const needsRelogin = Boolean(status?.needs_relogin)
  const stateText = !loggedIn ? '未登录' : needsRelogin ? '需重新登录' : '已登录'
  const badgeCls = !loggedIn
    ? 'inline-flex rounded-md border border-border bg-muted/60 px-2 py-0.5 text-[12px] font-semibold text-muted-foreground'
    : needsRelogin
      ? 'inline-flex rounded-md border border-destructive/25 bg-destructive/10 px-2 py-0.5 text-[12px] font-semibold text-destructive'
      : 'inline-flex rounded-md border border-emerald-500/20 bg-emerald-500/10 px-2 py-0.5 text-[12px] font-semibold text-[hsl(var(--success))]'

  return (
    <div className="flex flex-col gap-4">
      {err && (
        <div className="rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
      )}
      {notice && (
        <div className="rounded-lg border border-emerald-500/20 bg-[hsl(var(--success-bg))] px-3 py-2 text-[13px] text-[hsl(var(--success))]" role="status">{notice}</div>
      )}
      <div className="rounded-xl border border-border bg-card p-5 shadow-sm" data-slot="card">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-bold text-foreground">CodeArts 登录</h2>
          <button type="button" onClick={() => void loadStatus()} className="inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground" title="刷新">
            <RefreshCw className="size-3.5" /> 刷新
          </button>
        </div>
        <StatusTable status={status} stateText={stateText} badgeCls={badgeCls} />
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" onClick={start} disabled={starting || Boolean(session.sessionId)} className={primaryBtn}>
            {starting ? <Loader2 className="size-4 animate-spin" /> : <LogIn className="size-4" />}
            {loggedIn && !needsRelogin ? '重新登录' : '登录'}
          </button>
          {session.sessionId && (
            <button type="button" onClick={cancelLogin} className={dangerBtn}>
              <XCircle className="size-4" />
              取消登录
            </button>
          )}
          {loggedIn && (
            <button type="button" onClick={logout} disabled={loggingOut} className={dangerBtn}>
              {loggingOut ? <Loader2 className="size-4 animate-spin" /> : <LogOut className="size-4" />}
              退出登录
            </button>
          )}
        </div>
        {authUrl && (
          <AuthLinkPanel authUrl={authUrl} callbackHint={callbackHint} pollDetail={session.pollDetail} />
        )}
        {session.sessionId && (
          <CallbackImportForm
            sessionId={session.sessionId}
            onSettled={(result, message) => {
              if (result === 'ok') { session.resetSession(); setAuthUrl(''); setCallbackHint(''); setNotice(message) ; loadStatus() }
              else if (result === 'err') { session.resetSession(); setAuthUrl(''); setCallbackHint(''); setErr(message) }
            }}
          />
        )}
      </div>
    </div>
  )
}
