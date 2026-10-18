import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react'
import { Check, Copy, KeyRound, Loader2, Plus, Trash2, X } from 'lucide-react'
import { api } from '../api'

// 与后端 api_keys.NAME_MAX_LEN 保持一致
const NAME_MAX_LEN = 64

interface ApiKeyEntry {
  id: number
  key: string
  name: string
  enabled: boolean
  createdAt: string
}

interface ApiKeysData {
  keys: ApiKeyEntry[]
  require_api_key: string
}

const inputCls = 'w-full rounded-lg border border-input bg-background px-3 py-2 text-[13px] text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30'
const labelCls = 'mb-1.5 block text-[12px] font-semibold text-muted-foreground'
const primaryBtn = 'inline-flex items-center justify-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-primary-foreground transition-colors hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-60'
const ghostBtn = 'inline-flex items-center gap-1 rounded-md px-2 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60'
const dangerBtn = 'inline-flex items-center justify-center gap-1.5 rounded-lg bg-destructive px-3 py-2 text-[13px] font-semibold text-destructive-foreground transition-colors hover:bg-destructive/90 disabled:cursor-not-allowed disabled:opacity-60'

const badgeCls = (enabled: boolean) =>
  `inline-flex rounded-md border px-1.5 py-0.5 text-[11px] font-bold ${
    enabled
      ? 'border-emerald-500/20 bg-emerald-500/10 text-[hsl(var(--success))]'
      : 'border-border bg-muted/60 text-muted-foreground'
  }`

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current)
  }, [])

  const copy = async () => {
    let ok = false
    try {
      await navigator.clipboard.writeText(text)
      ok = true
    } catch {
      // 非安全上下文回退：临时 textarea + execCommand（失败时用户仍可手动选中复制）
      const area = document.createElement('textarea')
      area.value = text
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      try {
        ok = document.execCommand('copy')
      } catch {
        ok = false
      }
      document.body.removeChild(area)
    }
    // 两条路径都失败时不误报「已复制」，保留「复制」按钮供用户手动选中
    if (!ok) return
    setCopied(true)
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <button type="button" onClick={() => void copy()} className={ghostBtn} title="复制 API Key">
      {copied ? <Check className="size-3.5 text-[hsl(var(--success))]" /> : <Copy className="size-3.5" />}
      {copied ? '已复制' : '复制'}
    </button>
  )
}

/** 通用弹窗：锁定焦点在对话框内，打开期间背景内容 inert，关闭后归还焦点。 */
function Modal({
  label,
  onClose,
  children,
}: {
  label: string
  onClose: () => void
  children: ReactNode
}) {
  const dialogRef = useRef<HTMLDivElement>(null)
  // 渲染阶段捕获触发元素：子元素的 autoFocus 会在提交阶段先夺走焦点，effect 里再读就晚了
  const previousFocus = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null)

  useEffect(() => {
    const dialog = dialogRef.current
    // 保留子元素 autoFocus；没有 autoFocus 时聚焦对话框容器，避免焦点留在背景
    if (dialog && !dialog.contains(document.activeElement)) dialog.focus()
    return () => {
      const previous = previousFocus.current
      if (previous && document.contains(previous)) previous.focus()
    }
  }, [])

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'Tab') return
    const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
    )
    if (!focusables || focusables.length === 0) return
    const first = focusables[0]
    const last = focusables[focusables.length - 1]
    const active = document.activeElement
    // 焦点可能停在对话框容器本身（无 autoFocus 的弹窗）：Shift+Tab 同样应回绕到末尾
    if (event.shiftKey && (active === first || active === dialogRef.current)) {
      event.preventDefault()
      last.focus()
    } else if (!event.shiftKey && active === last) {
      event.preventDefault()
      first.focus()
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label={label}>
      <div
        className="absolute inset-0 bg-black/45 backdrop-blur-[2px]"
        aria-hidden="true"
        onClick={onClose}
      />
      <div
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="relative w-full max-w-sm rounded-xl border border-border bg-card p-5 shadow-2xl outline-none"
      >
        {children}
      </div>
    </div>
  )
}

export default function ApiKeyTab() {
  const [data, setData] = useState<ApiKeysData | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [createOpen, setCreateOpen] = useState(false)
  const [newName, setNewName] = useState('')
  const [createErr, setCreateErr] = useState('')
  const [created, setCreated] = useState<ApiKeyEntry | null>(null)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editName, setEditName] = useState('')
  const [pendingDelete, setPendingDelete] = useState<ApiKeyEntry | null>(null)

  const load = () =>
    api<ApiKeysData>('/api/admin/api-keys')
      .then((d) => {
        setData(d)
        setErr('')
      })
      .catch((e) => setErr(e.message))

  useEffect(() => { void load() }, [])

  // Esc 关闭生成 / 删除对话框
  useEffect(() => {
    if (!createOpen && !pendingDelete) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setCreateOpen(false)
      setPendingDelete(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [createOpen, pendingDelete])

  const create = () => {
    const name = newName.trim()
    if (!name) {
      setCreateErr('请输入名称')
      return
    }
    if (busy) return
    setBusy(true)
    api<{ key: ApiKeyEntry }>('/api/admin/api-keys', {
      method: 'POST',
      body: JSON.stringify({ name }),
    })
      .then((r) => {
        setCreateOpen(false)
        setNewName('')
        setCreateErr('')
        setCreated(r.key)
        void load()
      })
      .catch((e) => setCreateErr(e.message))
      .finally(() => setBusy(false))
  }

  const startRename = (entry: ApiKeyEntry) => {
    setEditingId(entry.id)
    setEditName(entry.name)
    setErr('')
  }

  const saveRename = (entry: ApiKeyEntry) => {
    const name = editName.trim()
    if (!name) {
      setErr('名称不能为空')
      return
    }
    if (busy) return
    setBusy(true)
    setErr('')
    api(`/api/admin/api-keys/${entry.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    })
      .then(() => {
        setEditingId(null)
        setEditName('')
        void load()
      })
      .catch((e) => setErr(e.message))
      .finally(() => setBusy(false))
  }

  const toggle = (entry: ApiKeyEntry) => {
    if (busy) return
    setBusy(true)
    setErr('')
    api(`/api/admin/api-keys/${entry.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled: !entry.enabled }),
    })
      .then(() => void load())
      .catch((e) => setErr(e.message))
      .finally(() => setBusy(false))
  }

  const remove = () => {
    if (!pendingDelete || busy) return
    setBusy(true)
    setErr('')
    api(`/api/admin/api-keys/${pendingDelete.id}`, { method: 'DELETE' })
      .then(() => {
        setPendingDelete(null)
        void load()
      })
      .catch((e) => {
        setPendingDelete(null)
        setErr(e.message)
      })
      .finally(() => setBusy(false))
  }

  if (err && !data) return <p className="text-[13px] text-muted-foreground">{err}</p>
  if (!data) return <p className="text-[13px] text-muted-foreground">加载中…</p>

  const requireOn = data.require_api_key !== '0'

  return (
    <>
      <div
        className="flex flex-col gap-4"
        // 弹窗打开时背景内容不可聚焦/交互（React 19 支持 inert 布尔属性）
        inert={createOpen || pendingDelete !== null}
      >
      <div
        className={`rounded-lg border px-3 py-2 text-[13px] ${
          requireOn
            ? 'border-border bg-muted/40 text-muted-foreground'
            : 'border-destructive/25 bg-destructive/10 text-destructive'
        }`}
        role={requireOn ? undefined : 'alert'}
      >
        {requireOn ? (
          <>
            强制鉴权已开启：<code className="font-mono">/v1/*</code> 请求需携带{' '}
            <code className="font-mono">x-api-key</code> 或 <code className="font-mono">Authorization: Bearer</code>。
            可在「配置」页签关闭。
          </>
        ) : (
          <>强制鉴权已关闭：<code className="font-mono">/v1/*</code> 不校验 API Key。可在「配置」页签重新开启。</>
        )}
      </div>

      {err && (
        <div className="rounded-lg border border-destructive/25 bg-destructive/10 px-3 py-2 text-[13px] text-destructive" role="alert">{err}</div>
      )}

      {created && (
        <div className="rounded-xl border border-emerald-500/25 bg-[hsl(var(--success-bg))] p-4">
          <div className="flex items-start justify-between gap-2">
            <div>
              <h3 className="text-[14px] font-bold text-foreground">API Key 已生成</h3>
              <p className="mt-0.5 text-[12px] text-muted-foreground">名称：{created.name}</p>
            </div>
            <button
              type="button"
              onClick={() => setCreated(null)}
              className="flex size-8 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground"
              aria-label="关闭"
            >
              <X className="size-4" />
            </button>
          </div>
          <div className="mt-3 flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2">
            <code className="min-w-0 flex-1 break-all font-mono text-[12px] text-foreground">{created.key}</code>
            <CopyButton text={created.key} />
          </div>
          <p className="mt-2 text-[12px] text-muted-foreground">
            该 key 已写入本地 <code className="font-mono">api-keys.json</code>，也可随时在上方列表查看与复制。
          </p>
        </div>
      )}

      <div className="rounded-xl border border-border bg-card shadow-sm" data-slot="card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2.5">
          <div>
            <h2 className="text-[14px] font-bold text-foreground">API Key</h2>
            <p className="mt-0.5 text-[12px] text-muted-foreground">{data.keys.length} 个</p>
          </div>
          <button
            type="button"
            onClick={() => {
              setCreateErr('')
              setCreateOpen(true)
            }}
            className={primaryBtn}
          >
            <Plus className="size-4" /> 生成 API Key
          </button>
        </div>

        {data.keys.length === 0 ? (
          <div className="flex flex-col items-center gap-3 px-4 py-10 text-center">
            <div className="flex size-12 items-center justify-center rounded-xl bg-primary/15">
              <KeyRound className="size-6 text-primary" />
            </div>
            <div>
              <p className="text-[14px] font-semibold text-foreground">还没有 API Key</p>
              <p className="mt-1 max-w-sm text-[12px] text-muted-foreground">
                点击右上角「生成 API Key」，把生成的 key 与 Base URL（本代理地址）一起配置到客户端即可开始使用。
              </p>
            </div>
          </div>
        ) : (
          <ul className="divide-y divide-border/60">
            {data.keys.map((entry) => (
              <li key={entry.id} className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    {editingId === entry.id ? (
                      <>
                        <input
                          value={editName}
                          onChange={(e) => setEditName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') saveRename(entry)
                            if (e.key === 'Escape') {
                              setEditingId(null)
                              setEditName('')
                            }
                          }}
                          maxLength={NAME_MAX_LEN}
                          aria-label="API Key 名称"
                          className="min-w-0 flex-1 rounded-md border border-input bg-background px-2 py-1 text-[13px] text-foreground outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/30 sm:max-w-[280px]"
                          autoFocus
                        />
                        <button type="button" className={ghostBtn} onClick={() => saveRename(entry)} disabled={busy}>保存</button>
                        <button type="button" className={ghostBtn} onClick={() => { setEditingId(null); setEditName('') }}>取消</button>
                      </>
                    ) : (
                      <>
                        <span className="text-[13px] font-semibold text-foreground">{entry.name}</span>
                        <span className={badgeCls(entry.enabled)}>{entry.enabled ? '启用' : '已停用'}</span>
                        <span className="text-[11px] text-muted-foreground">
                          #{entry.id}{entry.createdAt ? ` · ${entry.createdAt.replace('T', ' ')}` : ''}
                        </span>
                      </>
                    )}
                  </div>
                  <div className="mt-1 flex items-center gap-1.5">
                    <code className="min-w-0 break-all font-mono text-[12px] text-muted-foreground">{entry.key}</code>
                    <CopyButton text={entry.key} />
                  </div>
                </div>
                {editingId !== entry.id && (
                  <div className="flex shrink-0 flex-wrap items-center gap-1">
                    <button type="button" className={ghostBtn} onClick={() => startRename(entry)} disabled={busy}>改名</button>
                    <button type="button" className={ghostBtn} onClick={() => toggle(entry)} disabled={busy}>
                      {entry.enabled ? '停用' : '启用'}
                    </button>
                    <button
                      type="button"
                      className={`${ghostBtn} hover:bg-destructive/10 hover:text-destructive`}
                      onClick={() => setPendingDelete(entry)}
                      disabled={busy}
                    >
                      <Trash2 className="size-3.5" /> 删除
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      </div>

      {createOpen && (
        <Modal label="生成 API Key" onClose={() => setCreateOpen(false)}>
          <h3 className="text-[15px] font-bold text-foreground">生成 API Key</h3>
          <p className="mt-1 text-[12px] text-muted-foreground">给这个 key 起一个便于识别的名称，例如「笔记本」或「CI」。</p>
          <div className="mt-4">
            <label className={labelCls} htmlFor="new-api-key-name">名称（最多 {NAME_MAX_LEN} 字符）</label>
            <input
              id="new-api-key-name"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') create() }}
              maxLength={NAME_MAX_LEN}
              className={inputCls}
              placeholder="例如：笔记本"
              autoFocus
            />
          </div>
          {createErr && (
            <p className="mt-2 text-[12px] text-destructive" role="alert">{createErr}</p>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className={ghostBtn} onClick={() => setCreateOpen(false)} disabled={busy}>取消</button>
            <button type="button" className={primaryBtn} onClick={create} disabled={busy || !newName.trim()}>
              {busy && <Loader2 className="size-4 animate-spin" />}
              生成
            </button>
          </div>
        </Modal>
      )}

      {pendingDelete && (
        <Modal label="删除 API Key" onClose={() => setPendingDelete(null)}>
          <h3 className="text-[15px] font-bold text-foreground">删除 API Key</h3>
          <p className="mt-1 text-[13px] text-muted-foreground">
            确定删除「{pendingDelete.name}」？使用该 key 的客户端会立即收到 401，且无法恢复。
          </p>
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" className={ghostBtn} onClick={() => setPendingDelete(null)} disabled={busy}>取消</button>
            <button type="button" className={dangerBtn} onClick={remove} disabled={busy}>
              {busy && <Loader2 className="size-4 animate-spin" />}
              删除
            </button>
          </div>
        </Modal>
      )}
    </>
  )
}
