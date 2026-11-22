import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ProviderItem } from '../types'
import { Badge, Card, Dot, Empty, ErrorBox, Spinner, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

export default function Providers({ push }: { push: ToastPush }) {
  const [items, setItems] = useState<ProviderItem[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [dragName, setDragName] = useState<string | null>(null)
  const [quotaDetail, setQuotaDetail] = useState<Record<string, { local: Record<string, { count: number; remaining: number | null; reset_at: string | null }>; remote: Record<string, unknown> }>>({})

  const load = useCallback(async () => {
    try { setItems((await api.get<{ items: ProviderItem[] }>('/api/providers')).items); setError('') }
    catch (e) { setError((e as Error).message) }
  }, [])
  useEffect(() => { load() }, [load])

  async function toggle(p: ProviderItem) {
    setBusy(p.name)
    try {
      await api.patch(`/api/providers/${encodeURIComponent(p.name)}`, { enabled: !p.enabled })
      push(`${p.display_name} 已${p.enabled ? '禁用' : '启用'}`); load()
    } catch (e) { push((e as Error).message, 'error') }
    finally { setBusy(null) }
  }

  async function test(p: ProviderItem) {
    setBusy(p.name)
    try {
      const r = await api.post<{ ok: boolean; message: string }>(`/api/providers/${encodeURIComponent(p.name)}/test`)
      push(r.ok ? `${p.display_name} 连接成功` : `${p.display_name}: ${r.message}`, r.ok ? 'ok' : 'error')
      load()
    } catch (e) { push((e as Error).message, 'error') }
    finally { setBusy(null) }
  }

  async function resetHealth(p: ProviderItem) {
    try { await api.post(`/api/providers/${encodeURIComponent(p.name)}/reset-health`); push('已解除熔断'); load() }
    catch (e) { push((e as Error).message, 'error') }
  }

  async function viewQuota(p: ProviderItem) {
    try {
      type QuotaInfo = { local: Record<string, { count: number; remaining: number | null; reset_at: string | null }>; remote: Record<string, unknown> }
      const r = await api.get<QuotaInfo>(`/api/providers/${encodeURIComponent(p.name)}/quota`)
      setQuotaDetail((prev) => ({ ...prev, [p.name]: r }))
    } catch (e) { push((e as Error).message, 'error') }
  }

  // 拖拽调整优先级：放到目标位置，后端按 index 重排。
  async function dropOn(target: string) {
    if (!dragName || dragName === target) { setDragName(null); return }
    const ordered = [...(items || [])].sort((a, b) => a.priority - b.priority).map((p) => p.name)
    const from = ordered.indexOf(dragName)
    const to = ordered.indexOf(target)
    if (from < 0 || to < 0) { setDragName(null); return }
    ordered.splice(from, 1)
    ordered.splice(to, 0, dragName)
    setDragName(null)
    try {
      for (let i = 0; i < ordered.length; i++) {
        await api.patch(`/api/providers/${encodeURIComponent(ordered[i])}`, { priority: i })
      }
      push('优先级已更新'); load()
    } catch (e) { push((e as Error).message, 'error') }
  }

  if (items === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  const sorted = [...items].sort((a, b) => a.priority - b.priority)

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>字幕源</h1>
          <p className="page-sub">卡片可拖拽调整优先级 · 启停、连接测试与熔断管理即时生效</p>
        </div>
        <button className="btn" onClick={load}>刷新</button>
      </div>

      {sorted.length === 0 ? <Card><Empty text="没有字幕源" /></Card> : (
        <div className="provider-list">
          {sorted.map((p, idx) => {
            const q = quotaDetail[p.name]
            const isAbnormal = p.state === 'BROKEN' || p.state === 'THROTTLED'
            return (
              <div key={p.name} className={`provider-card ${isAbnormal ? 'abnormal' : ''} ${dragName === p.name ? 'dragging' : ''}`}
                draggable onDragStart={() => setDragName(p.name)} onDragOver={(e) => e.preventDefault()}
                onDrop={() => dropOn(p.name)}>
                <div className="provider-grip" title="拖拽调整优先级">⋮⋮</div>
                <div className="provider-index">{idx + 1}</div>
                <div className="provider-main">
                  <div className="provider-title">
                    <strong>{p.display_name}</strong>
                    <Badge tone={p.kind === 'custom' ? 'accent' : 'muted'}>{p.kind === 'custom' ? '自建' : '原生'}</Badge>
                    <Dot tone={p.enabled ? (isAbnormal ? 'error' : 'ok') : 'muted'} pulse={p.enabled && !isAbnormal} />
                    <span className="provider-state">{stateLabel(p.state)}</span>
                  </div>
                  <div className="provider-meta">
                    <span>语言: {p.languages.map(langLabel).join(' / ')}</span>
                    <span>{p.credential_configured === null ? '无需凭证' : p.credential_configured ? '凭证已配置' : '凭证未配置'}</span>
                    {p.fail_count > 0 && <span className="warn-text">失败 {p.fail_count} 次</span>}
                    {p.cooldown_until && <span className="warn-text">冷却至 {formatTime(p.cooldown_until)}</span>}
                  </div>
                  {q && <div className="provider-quota">
                    <span className="muted">本地配额：</span>
                    {Object.keys(q.local || {}).length === 0 ? '无记录' : Object.entries(q.local).map(([action, v]) => {
                      const info = v as { count: number; remaining: number | null; reset_at: string | null }
                      return <span key={action} className="score-chip">{action}: {info.count}{info.remaining != null ? ` / 剩 ${info.remaining}` : ''}</span>
                    })}
                    {q.remote && Object.keys(q.remote).length > 0 && <span className="muted">远端已同步</span>}
                  </div>}
                </div>
                <div className="provider-actions">
                  <button className={`toggle ${p.enabled ? 'on' : ''}`} onClick={() => toggle(p)} disabled={busy === p.name}
                    title={p.enabled ? '点击禁用' : '点击启用'}><span /></button>
                  <button className="btn btn-sm" onClick={() => test(p)} disabled={busy === p.name}>测试</button>
                  <button className="btn btn-sm btn-ghost" onClick={() => viewQuota(p)}>配额</button>
                  {isAbnormal && <button className="btn btn-sm btn-ghost" onClick={() => resetHealth(p)}>解熔断</button>}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <p className="hint-block">
        说明：凭证请在「设置 → 凭证」中填写；原生源来自 Subliminal 插件，自建源（ASSRT / SubDL / Zimuku）
        使用各自官方协议。优先级仅影响同级评分的先后，实际选择仍以匹配分数为准。
      </p>
    </div>
  )
}

function stateLabel(s: string): string {
  const map: Record<string, string> = { OK: '健康', THROTTLED: '限流中', BROKEN: '已熔断' }
  return map[s] || s
}
function formatTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) } catch { return iso }
}
function langLabel(code: string): string {
  const map: Record<string, string> = { zh: '中', en: '英', zho: '中', eng: '英', ja: '日', ko: '韩' }
  return map[code] || code
}
