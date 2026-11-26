// 统一 API 客户端：处理 401 跳转、错误提取、SSE 订阅。

import type { Decision } from './types'

export interface JobStreamPayload {
  state?: string
  progress?: number
  error_message?: string | null
  items?: Decision[]
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

// 后端 detail 可能是字符串、{detail:...}、或 422 校验错误数组，统一提取可读文案。
function detailText(data: unknown, fallback: string): string {
  if (typeof data === 'string') return data
  if (data && typeof data === 'object') {
    const detail = (data as { detail?: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail.map((d) => {
        if (d && typeof d === 'object') {
          const loc = (d as { loc?: unknown[] }).loc
          const msg = (d as { msg?: string }).msg || '参数错误'
          return loc && loc.length > 1 ? `${String(loc[loc.length - 1])}: ${msg}` : msg
        }
        return String(d)
      }).join('；')
    }
    if (detail && typeof detail === 'object') return JSON.stringify(detail)
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(path, {
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
      ...init,
    })
  } catch (e) {
    throw new ApiError(`网络错误: ${(e as Error).message}`, 0)
  }

  const text = await resp.text()
  let data: unknown = null
  if (text) {
    try { data = JSON.parse(text) } catch { data = text }
  }

  if (resp.status === 401) {
    // 登录接口的 401 是「账号/密码错误」，须保留后端文案；其他接口的 401 才是会话过期。
    const isLogin = path === '/api/auth/login'
    if (!isLogin && !location.pathname.startsWith('/login')) {
      location.href = '/login'
    }
    const detail = isLogin ? detailText(data, '用户名或密码错误') : '未登录或会话已过期'
    throw new ApiError(detail, 401)
  }

  if (!resp.ok) {
    throw new ApiError(detailText(data, resp.statusText) || `请求失败 (${resp.status})`, resp.status)
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  // multipart 上传：不设置 Content-Type，由浏览器自动生成 boundary。
  upload: <T>(path: string, form: FormData) =>
    fetch(path, { method: 'POST', credentials: 'same-origin', body: form })
      .then(async (resp) => {
        if (resp.status === 401) { location.href = '/login'; throw new ApiError('未登录', 401) }
        const text = await resp.text()
        let data: unknown = null
        if (text) { try { data = JSON.parse(text) } catch { data = text } }
        if (!resp.ok) throw new ApiError(detailText(data, resp.statusText), resp.status)
        return data as T
      }),

  // SSE：cookie 自动随同源请求发送，订阅任务进度。
  streamJob(
    jobId: number,
    onEvent: (payload: JobStreamPayload, type: string) => void,
    onError?: (e: Event) => void,
  ): () => void {
    const source = new EventSource(`/api/jobs/${jobId}/events`)
    let settled = false // 是否已收到终态；收到后主动关闭，避免 EventSource 自动重连重复推送
    const forward = (type: string) => (e: MessageEvent) => {
      try {
        onEvent(JSON.parse(e.data), type)
        if (type === 'job.done') {
          settled = true
          source.close()
        }
      } catch { /* 忽略坏帧 */ }
    }
    source.addEventListener('job.progress', forward('job.progress') as EventListener)
    source.addEventListener('job.decisions', forward('job.decisions') as EventListener)
    source.addEventListener('job.done', forward('job.done') as EventListener)
    source.addEventListener('error', (e) => {
      if (settled) return // 主动关闭后的 error 无需处理
      if (e instanceof MessageEvent) {
        try { onEvent(JSON.parse(e.data), 'error') } catch { /* ignore */ }
      } else {
        onError?.(e)
      }
    })
    return () => { settled = true; source.close() }
  },
}
