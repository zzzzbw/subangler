import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ProviderItem } from '../types'
import { Badge, Card, Dot, Empty, ErrorBox, Spinner, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']
type CredField = ProviderItem['credential_fields'][number]

export default function Providers({ push }: { push: ToastPush }) {
  const [items, setItems] = useState<ProviderItem[] | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [dragName, setDragName] = useState<string | null>(null)
  const [quotaDetail, setQuotaDetail] = useState<Record<string, { local: Record<string, { count: number; remaining: number | null; reset_at: string | null }>; remote: Record<string, unknown> }>>({})
  const [credOpen, setCredOpen] = useState<string | null>(null)
  const [credDraft, setCredDraft] = useState<Record<string, string>>({})
  const [credRevealed, setCredRevealed] = useState<Record<string, string>>({})
  const [credSaving, setCredSaving] = useState(false)
  const [credTest, setCredTest] = useState<Record<string, string>>({})

  const load = useCallback(async () => {
    try {
      const data = (await api.get<{ items: ProviderItem[] }>('/api/providers')).items
      setItems(data)
      // 凭证输入框以掩码值作为初始草稿；已展开的不覆盖用户输入。
      setCredDraft((prev) => {
        const next = { ...prev }
        for (const p of data) {
          for (const f of p.credential_fields) {
            if (next[f.key] === undefined) next[f.key] = f.masked
          }
        }
        return next
      })
      setError('')
    } catch (e) { setError((e as Error).message) }
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

  function openCredentials(p: ProviderItem) {
    if (credOpen === p.name) { setCredOpen(null); return }
    setCredOpen(p.name)
    setCredTest({})
    // 展开时用最新的掩码重置草稿，丢弃上一次未保存的编辑。
    const draft: Record<string, string> = {}
    for (const f of p.credential_fields) draft[f.key] = f.masked
    setCredDraft((prev) => ({ ...prev, ...draft }))
    setCredRevealed({})
  }

  function setField(key: string, value: string) {
    setCredDraft((prev) => ({ ...prev, [key]: value }))
    // 用户输入新值后，明文字段失效。
    if (credRevealed[key] !== undefined) {
      setCredRevealed((prev) => { const next = { ...prev }; delete next[key]; return next })
    }
  }

  async function toggleReveal(f: CredField) {
    if (credRevealed[f.key] !== undefined) {
      setCredRevealed((prev) => { const next = { ...prev }; delete next[f.key]; return next })
      return
    }
    const current = credDraft[f.key] || ''
    if (current && !current.includes('****')) {
      setCredRevealed((prev) => ({ ...prev, [f.key]: current }))
      return
    }
    try {
      const r = await api.post<{ value: string }>('/api/config/reveal', { key: f.key })
      setCredRevealed((prev) => ({ ...prev, [f.key]: r.value || '' }))
    } catch (e) { push((e as Error).message, 'error') }
  }

  function fieldValue(f: CredField): string {
    return credRevealed[f.key] ?? credDraft[f.key] ?? ''
  }

  async function saveCredentials(p: ProviderItem) {
    const values: Record<string, string> = {}
    for (const f of p.credential_fields) {
      const v = fieldValue(f)
      // 掩码未改动则不写回，避免用掩码覆盖真实凭证。
      if (v !== f.masked && !(f.configured && !v)) values[f.key] = v
    }
    if (Object.keys(values).length === 0) { push('没有需要保存的改动'); return }
    setCredSaving(true)
    try {
      await api.patch('/api/config', { values })
      push(`${p.display_name} 凭证已保存，即时生效`)
      // 清除旧草稿与明文，load() 会用最新掩码重新填充，改动标记随之复位。
      setCredRevealed({})
      setCredDraft((prev) => {
        const next = { ...prev }
        for (const f of p.credential_fields) delete next[f.key]
        return next
      })
      await load()
      setCredOpen(p.name)
    } catch (e) { push((e as Error).message, 'error') }
    finally { setCredSaving(false) }
  }

  async function testCredential(f: CredField) {
    const value = fieldValue(f)
    const typed = credDraft[f.key] ?? f.masked
    // 编辑过（输入新值或点过「显示」）用输入框明文；否则让后端用已保存凭证探测，
    // 避免掩码状态下误判为「未填写」，也避免明文传到前端。
    const edited = typed !== f.masked || credRevealed[f.key] !== undefined
    if (edited && (!value || value.includes('****'))) { push('请先填写凭证', 'error'); return }
    if (!edited && !f.configured) { push('请先填写凭证', 'error'); return }
    const provider = f.test_provider
    if (!provider) { push('该凭证不支持单独探测，请保存后用「测试」按钮验证', 'error'); return }
    setCredTest((s) => ({ ...s, [f.key]: '测试中…' }))
    const body: Record<string, string> = { provider }
    if (edited) {
      if (provider === 'assrt') body.token = value; else body.api_key = value
    }
    try {
      const r = await api.post<{ ok: boolean; detail: string }>('/api/config/test-connection', body)
      setCredTest((s) => ({ ...s, [f.key]: r.ok ? '✓ 成功' : `✗ ${r.detail}` }))
      push(r.ok ? '连接成功' : `连接失败: ${r.detail}`, r.ok ? 'ok' : 'error')
    } catch (e) {
      setCredTest((s) => ({ ...s, [f.key]: `✗ ${(e as Error).message}` }))
    }
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
          <p className="page-sub">卡片可拖拽调整优先级 · 凭证、启停、连接测试与熔断管理即时生效</p>
        </div>
        <button className="btn" onClick={load}>刷新</button>
      </div>

      {sorted.length === 0 ? <Card><Empty text="没有字幕源" /></Card> : (
        <div className="provider-list">
          {sorted.map((p, idx) => {
            const q = quotaDetail[p.name]
            const isAbnormal = p.state === 'BROKEN' || p.state === 'THROTTLED'
            const hasCred = p.credential_fields.length > 0
            const draftChanged = p.credential_fields.some((f) => (credDraft[f.key] ?? f.masked) !== f.masked)
            return (
              <div key={p.name} className={`provider-card ${isAbnormal ? 'abnormal' : ''} ${dragName === p.name ? 'dragging' : ''}`}>
                <div className="provider-card-row" draggable onDragStart={() => setDragName(p.name)} onDragOver={(e) => e.preventDefault()} onDrop={() => dropOn(p.name)}>
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
                    {hasCred && <button className={`btn btn-sm ${draftChanged ? 'btn-primary' : ''}`} onClick={() => openCredentials(p)}
                      title="管理该源的凭证">{credOpen === p.name ? '收起凭证' : '凭证'}</button>}
                    <button className="btn btn-sm" onClick={() => test(p)} disabled={busy === p.name}>测试</button>
                    <button className="btn btn-sm btn-ghost" onClick={() => viewQuota(p)}>配额</button>
                    {isAbnormal && <button className="btn btn-sm btn-ghost" onClick={() => resetHealth(p)}>解熔断</button>}
                  </div>
                </div>
                {credOpen === p.name && hasCred && (
                  <div className="credential-panel">
                    <div className="credential-hint">凭证加密存储，仅在该源需要时读取；保存后即时生效，无需重启。</div>
                    {p.credential_fields.map((f) => (
                      <div key={f.key} className="credential-field">
                        <label>{f.label}</label>
                        <div className="credential-controls">
                          <input className="config-input" type={credRevealed[f.key] !== undefined ? 'text' : 'password'}
                            value={fieldValue(f)} placeholder={f.configured ? '已配置（留空清除）' : '未设置'}
                            onChange={(e) => setField(f.key, e.target.value)} />
                          <button className="btn btn-sm btn-ghost" onClick={() => toggleReveal(f)}
                            title={credRevealed[f.key] !== undefined ? '隐藏' : '显示'}>
                            {credRevealed[f.key] !== undefined ? '隐藏' : '显示'}
                          </button>
                          {f.test_provider && (
                            <button className="btn btn-sm" onClick={() => testCredential(f)}>{credTest[f.key] || '测试'}</button>
                          )}
                        </div>
                      </div>
                    ))}
                    <div className="credential-actions">
                      <button className="btn btn-sm btn-primary" onClick={() => saveCredentials(p)} disabled={credSaving}>
                        {credSaving ? '保存中…' : '保存凭证'}
                      </button>
                      <button className="btn btn-sm btn-ghost" onClick={() => openCredentials(p)}>取消</button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      <p className="hint-block">
        说明：凭证可直接在各源卡片内「凭证」处管理；原生源来自 Subliminal 插件，自建源（ASSRT / SubDL / Zimuku）
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
