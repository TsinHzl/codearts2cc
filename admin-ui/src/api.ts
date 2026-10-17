// admin 前端 fetch 封装：统一 JSON 请求/解析与错误透传（与原 api.js 行为逐行等价）
export class AuthError extends Error {}

export type ApiOptions = RequestInit & { headers?: Record<string, string> }

// 从任意响应体提取字符串错误消息；非对象或非字符串时返回空串。
const errorMessage = (value: unknown): string => {
  if (value === null || typeof value !== 'object') return ''
  const message = (value as { error?: unknown }).error
  return typeof message === 'string' ? message : ''
}

export async function api<T = unknown>(url: string, opts: ApiOptions = {}): Promise<T> {
  const { headers, ...rest } = opts
  const r = await fetch(url, {
    ...rest,
    // 默认 JSON 请求头，调用方传入的同名头优先
    headers: { 'Content-Type': 'application/json', ...(headers ?? {}) },
  })
  // 解析失败与合法 JSON null 都收敛为 null，避免错误分支读取属性时抛 TypeError
  const parsed: unknown = await r.json().catch(() => null)
  if (r.status === 401) throw new AuthError(errorMessage(parsed) || '未登录或会话已过期')
  if (!r.ok) throw new Error(errorMessage(parsed) || 'HTTP ' + r.status)
  return (parsed ?? {}) as T
}
