// admin 前端 fetch 封装：统一 JSON 请求/解析与错误透传（与原 api.js 行为逐行等价）
export class AuthError extends Error {}

export type ApiOptions = RequestInit & { headers?: Record<string, string> }

export async function api<T = unknown>(url: string, opts: ApiOptions = {}): Promise<T> {
  const r = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  const d = await r.json().catch(() => ({}) as Record<string, never>)
  if (r.status === 401) throw new AuthError(d.error || '未登录或会话已过期')
  if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status)
  return d as T
}
