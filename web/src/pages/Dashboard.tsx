import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import type { Video, ProviderItem, HealthReport, ConfigItem } from '../types'
import { Badge, Card, Dot, Empty, ErrorBox, Spinner, humanizeReason, langLabel, usePolling, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

export default function Dashboard({ push }: { push: ToastPush }) {
  const [videos, setVideos] = useState<Video[] | null>(null)
  const [providers, setProviders] = useState<ProviderItem[]>([])
  const [health, setHealth] = useState<HealthReport | null>(null)
  const [languages, setLanguages] = useState<string[]>(['zh', 'en'])
  const [error, setError] = useState('')
  const [triggerBusy, setTriggerBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const [vRes, pRes, hRes, cRes] = await Promise.all([
        api.get<{ items: Video[] }>('/api/videos?limit=1000'),
        api.get<{ items: ProviderItem[] }>('/api/providers'),
        api.get<HealthReport>('/api/health'),
        api.get<{ items: ConfigItem[] }>('/api/config'),
      ])
      setVideos(vRes.items)
      setProviders(pRes.items)
      setHealth(hRes)
      const langItem = cRes.items.find((i) => i.key === 'general.languages')
      if (Array.isArray(langItem?.value)) setLanguages(langItem.value as string[])
      setError('')
    } catch (e) { setError((e as Error).message) }
  }, [])

  useEffect(() => { load() }, [load])
  usePolling(load, 15000)

  if (videos === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  const missing = videos.filter((v) => isMissing(v))
  const hasSub = videos.filter((v) => v.status === 'has_subtitle')
  const byLang: Record<string, number> = {}
  for (const v of videos) {
    for (const lang of languages) {
      if (!v.subtitles.some((s) => s.language === lang || s.language.startsWith(lang))) {
        byLang[lang] = (byLang[lang] || 0) + 1
      }
    }
  }
  const coverage = videos.length ? Math.round((hasSub.length / videos.length) * 100) : 0
  const enabledProviders = providers.filter((p) => p.enabled)
  const brokenProviders = providers.filter((p) => p.state === 'BROKEN' || p.state === 'THROTTLED')

  async function quickScanAll() {
    setTriggerBusy(true)
    try {
      const libs = await api.get<{ items: { id: number }[] }>('/api/libraries')
      if (!libs.items.length) { push('请先在「媒体库」添加目录', 'error'); return }
      const results = await Promise.all(libs.items.map((l) => api.post<{ job_id: number }>(`/api/libraries/${l.id}/scan`)))
      push(`已触发 ${results.length} 个扫描任务`)
    } catch (e) { push((e as Error).message, 'error') }
    finally { setTriggerBusy(false) }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>总览</h1>
          <p className="page-sub">一屏掌握字幕覆盖、字幕源健康与系统状态</p>
        </div>
        <div className="head-actions">
          <button className="btn" onClick={load}>刷新</button>
          <button className="btn btn-primary" onClick={quickScanAll} disabled={triggerBusy}>
            {triggerBusy ? '触发中…' : '⟳ 扫描全部媒体库'}
          </button>
        </div>
      </div>

      <div className="stat-grid">
        <StatCard label="视频总数" value={videos.length} tone="accent" sub={`${hasSub.length} 集已有字幕`} />
        <StatCard label="缺字幕" value={missing.length} tone={missing.length ? 'warn' : 'ok'} sub="需要下载或人工处理" to="/videos?status=missing" />
        <StatCard label="覆盖率" value={`${coverage}%`} tone={coverage >= 80 ? 'ok' : 'warn'} sub="至少有一种字幕" />
        <StatCard label="可用字幕源" value={`${enabledProviders.length}/${providers.length}`} tone={enabledProviders.length ? 'ok' : 'error'} sub={`${brokenProviders.length} 个异常`} to="/providers" />
      </div>

      <div className="grid-2">
        <Card title="语言缺口" subtitle="按语言统计缺字幕的剧集数" actions={<Link className="link" to="/videos">查看剧集 →</Link>}>
          {languages.length === 0 ? <Empty text="未配置语言" hint="前往设置添加" /> : (
            <div className="lang-gaps">
              {languages.map((lang) => (
                <div key={lang} className="lang-gap-row">
                  <span className="lang-name">{langLabel(lang)}</span>
                  <div className="bar"><div className="bar-fill" style={{ width: `${videos.length ? ((byLang[lang] || 0) / videos.length) * 100 : 0}%` }} /></div>
                  <span className="lang-count">{byLang[lang] || 0} 缺</span>
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card title="系统自检" subtitle="依赖、权限与网络连通性" actions={<Link className="link" to="/settings">查看配置 →</Link>}>
          {!health ? <Spinner /> : (
            <>
              <div className="health-summary">
                <span><Dot tone="ok" /> {health.summary.ok} 正常</span>
                <span><Dot tone="warn" /> {health.summary.warn} 警告</span>
                <span><Dot tone="error" /> {health.summary.fail} 失败</span>
              </div>
              <div className="check-list">
                {health.checks.filter((c) => c.status !== 'ok').slice(0, 5).map((c) => (
                  <div key={c.name} className="check-row">
                    <Dot tone={c.status} />
                    <span className="check-name">{c.name}</span>
                    <span className="check-detail">{humanizeReason(c.detail)}</span>
                  </div>
                ))}
                {health.checks.every((c) => c.status === 'ok') && <Empty text="全部检查通过" />}
              </div>
            </>
          )}
        </Card>
      </div>

      <Card title="最需要关注的剧集" subtitle="缺字幕的剧集，点击前往下载"
        actions={<Link className="link" to="/videos">全部剧集 →</Link>}>
        {missing.length === 0 ? <Empty text="没有缺字幕的剧集" hint="扫描媒体库后会在此显示" /> : (
          <div className="video-mini-list">
            {missing.slice(0, 6).map((v) => (
              <Link key={v.id} to={`/videos?video=${v.id}`} className="video-mini">
                <div className="video-mini-title">{v.title || shortName(v.path)} {v.season && <Badge tone="muted">S{v.season}{v.episode ? `E${v.episode}` : ''}</Badge>}</div>
                <div className="video-mini-sub">{shortName(v.path)}</div>
              </Link>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function StatCard({ label, value, sub, tone, to }: {
  label: string; value: string | number; sub?: string; tone?: string; to?: string
}) {
  const inner = (
    <>
      <div className="stat-label">{label}</div>
      <div className={`stat-value stat-${tone || 'default'}`}>{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </>
  )
  return to ? <Link className="stat-card link-card" to={to}>{inner}</Link> : <div className="stat-card">{inner}</div>
}

function isMissing(v: Video): boolean {
  if (v.status === 'has_subtitle' || v.status === 'skipped') return false
  return v.status === 'missing' || v.status === 'pending' || v.status === 'needs_attention'
}

function shortName(path: string): string {
  const parts = path.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || path
}
