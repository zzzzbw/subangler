// 通用 UI 小组件：徽标、状态点、卡片、加载/空态、Toast。
import { useEffect, useState, type ReactNode } from 'react'

export function Badge({ children, tone = 'default', title }: { children: ReactNode; tone?: string; title?: string }) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>
}

export function Dot({ tone = 'ok', pulse = false }: { tone?: string; pulse?: boolean }) {
  return <span className={`dot dot-${tone} ${pulse ? 'dot-pulse' : ''}`} />
}

export function Card({ title, subtitle, actions, children }: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode
}) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {subtitle && <p className="card-subtitle">{subtitle}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  )
}

export function Spinner() {
  return <div className="center-state"><span className="spinner" /><span>加载中…</span></div>
}

export function Empty({ text = '暂无数据', hint }: { text?: string; hint?: string }) {
  return (
    <div className="center-state empty-state">
      <span className="empty-icon">◌</span>
      <span>{text}</span>
      {hint && <small>{hint}</small>}
    </div>
  )
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-box">
      <span>{message}</span>
      {onRetry && <button className="btn btn-sm" onClick={onRetry}>重试</button>}
    </div>
  )
}

export interface ToastMsg { id: number; text: string; tone: 'ok' | 'error' }

export function useToasts() {
  const [toasts, setToasts] = useState<ToastMsg[]>([])
  const push = (text: string, tone: 'ok' | 'error' = 'ok') => {
    const id = Date.now() + Math.random()
    setToasts((prev) => [...prev, { id, text, tone }])
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 3500)
  }
  return { toasts, push }
}

export function ToastStack({ toasts }: { toasts: ToastMsg[] }) {
  return (
    <div className="toast-stack">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.tone}`}>
          <span className="toast-icon">{t.tone === 'ok' ? '✓' : '!'}</span>
          {t.text}
        </div>
      ))}
    </div>
  )
}

// 把后端错误文案转成易读提示（设计文档要求“失败原因是人话”）。
export function humanizeReason(reason: string | null | undefined): string {
  if (!reason) return '—'
  const r = String(reason)
  if (/quota|限额|配额|limit/i.test(r)) return '配额已用尽或达到限速，稍后自动重试'
  if (/captcha|验证码/i.test(r)) return '站点需要验证码，请在浏览器中手动处理'
  if (/below_threshold|评分|threshold/i.test(r)) return r.includes('<') ? `候选评分不达标（${r}），疑似版本或语言不符` : '候选评分低于阈值，未自动采用'
  if (/throttl|熔断|cooldown/i.test(r)) return '字幕源暂时不可用（已熔断冷却），已切换其他源'
  if (/no_candidate|无候选/i.test(r)) return '所有字幕源均没有找到匹配候选'
  if (/embedded|内嵌/i.test(r)) return '视频已有内嵌字幕，已按设置跳过'
  if (/skip|已有|has_subtitle/i.test(r)) return '已存在字幕，按设置跳过'
  return r
}

export function langLabel(code: string): string {
  const map: Record<string, string> = {
    zh: '简体中文', zho: '简体中文', 'zh-CN': '简体中文',
    en: '英文', eng: '英文',
    yue: '粤语', zh_Hant: '繁体中文', jpn: '日语', kor: '韩语',
  }
  return map[code] || code
}

export function stateTone(state: string): string {
  if (state === 'SUCCESS' || state === 'ok') return 'ok'
  if (state === 'PARTIAL' || state === 'warn' || state === 'RUNNING') return 'warn'
  if (state === 'FAILED' || state === 'fail' || state === 'BROKEN') return 'error'
  if (state === 'PENDING' || state === 'SKIPPED' || state === 'THROTTLED') return 'muted'
  return 'default'
}

export function timeAgo(iso: string | null): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`
  return new Date(iso).toLocaleDateString('zh-CN')
}

export function usePolling(fn: () => void, ms: number, enabled = true) {
  useEffect(() => {
    if (!enabled) return
    const id = setInterval(fn, ms)
    return () => clearInterval(id)
  }, [fn, ms, enabled])
}
