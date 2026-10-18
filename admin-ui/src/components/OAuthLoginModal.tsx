import { useCallback, useEffect, useRef, useState } from 'react'
import { ExternalLink, Loader2, LogIn, X, XCircle } from 'lucide-react'
import { api } from '../api'

interface PollData {
  status?: 'pending' | 'ok' | 'err'
  error?: string
  detail?: string
}

interface StartData {
  session_id?: string
  auth_url?: string
  callback_hint?: string
}

const inputCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30'
const labelCls = 'mb-1.5 block text-[12px] font-semibold text-muted-foreground'
const primaryBtn = 'inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60'
const dangerBtn = 'inline-flex items-center gap-1.5 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-[13px] font-semibold text-destructive transition-colors hover:bg-destructive/15 disabled:cursor-not-allowed disabled:opacity-60'
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

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
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

/** 概览页「登录」按钮弹出的 OAuth 登录模态框：登录成功后 onSettled 通知父级刷新概览并自动关闭。 */
export default function OAuthLoginModal({ open, onClose, onSettled }: {
  open: boolean
  onClose: () => void
  onSettled: (result: 'ok' | 'logout') => void
}) {
  const [err, setErr] = useState('')
  const [notice, setNotice] = useState('')
  const [starting, setStarting] = useState(false)
  const [authUrl, setAuthUrl] = useState('')
  const [callbackHint, setCallbackHint] = useState('')
  const closeBtnRef = useRef<HTMLButtonElement | null>(null)
  // openRef：start() 异步回调感知模态框已关闭，避免关框后仍启动后台轮询（CR #1）
  const openRef = useRef(open)
  openRef.current = open

  const session = useOAuthSession({
    onSuccess: () => {
      setAuthUrl('')
      setCallbackHint('')
      setErr('')
      onSettled('ok')
    },
    onError: (msg) => { setErr(msg); setAuthUrl(''); setCallbackHint('') },
  })

  // 打开时聚焦关闭按钮；关闭/卸载时锁定与恢复 body 滚动（与 Layout More sheet 同款处理）。
  useEffect(() => {
    if (!open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    closeBtnRef.current?.focus()
    return () => { document.body.style.overflow = prev }
  }, [open])

  useEffect(() => {
    if (!open) return
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { session.resetSession(); onClose() }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, onClose])

  // 每次打开重置为初始态（错误/通知/残留授权链接/轮询会话全清）。
  useEffect(() => {
    if (open) return
    session.resetSession()
    setErr('')
    setNotice('')
    setAuthUrl('')
    setCallbackHint('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  if (!open) return null

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
        // 模态框已被关闭（Escape/背景/X）：关掉弹窗、不启动轮询会话
        if (!openRef.current) {
          popup?.close()
          return
        }
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

  const cancelLogin = () => {
    session.resetSession()
    setAuthUrl('')
    setCallbackHint('')
    setNotice('已取消登录')
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label="CodeArts OAuth 登录">
      <button
        type="button"
        className="absolute inset-0 bg-black/45 backdrop-blur-[2px]"
        aria-label="关闭"
        onClick={() => { session.resetSession(); onClose() }}
      />
      <div className="relative w-full max-w-lg rounded-2xl border border-border bg-card shadow-2xl animate-in fade-in-0 zoom-in-95 duration-200">
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-base font-bold text-foreground">CodeArts 登录</h2>
          <button
            type="button"
            ref={closeBtnRef}
            onClick={() => { session.resetSession(); onClose() }}
            className="flex size-9 items-center justify-center rounded-xl text-muted-foreground transition-colors hover:bg-muted/70 hover:text-foreground"
            aria-label="关闭"
          >
            <X className="size-5" />
          </button>
        </div>
        <div className="max-h-[calc(85dvh-4rem)] overflow-y-auto px-5 py-4">
          {err && (
            <div className="mb-3 rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
          )}
          {notice && (
            <div className="mb-3 rounded-lg border border-emerald-500/20 bg-[hsl(var(--success-bg))] px-3 py-2 text-[13px] text-[hsl(var(--success))]" role="status">{notice}</div>
          )}
          <p className="text-[13px] leading-relaxed text-muted-foreground">
            点击「登录」后在浏览器完成华为云账号授权，授权完成后本窗口自动关闭；浏览器不在本机时可将最终回调地址粘贴到下方导入框。
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button type="button" onClick={start} disabled={starting || Boolean(session.sessionId)} className={primaryBtn}>
              {starting ? <Loader2 className="size-4 animate-spin" /> : <LogIn className="size-4" />}
              登录
            </button>
            {session.sessionId && (
              <button type="button" onClick={cancelLogin} className={dangerBtn}>
                <XCircle className="size-4" />
                取消登录
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
                if (result === 'ok') {
                  session.resetSession()
                  setAuthUrl('')
                  setCallbackHint('')
                  setErr('')
                  onSettled('ok')
                } else if (result === 'err') {
                  session.resetSession()
                  setAuthUrl('')
                  setCallbackHint('')
                  setErr(message)
                }
              }}
            />
          )}
        </div>
      </div>
    </div>
  )
}
