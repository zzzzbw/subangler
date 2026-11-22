import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Video, Library } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, langLabel, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

type StatusFilter = 'all' | 'missing' | 'has_subtitle' | 'needs_attention'

export default function Videos({ push }: { push: ToastPush }) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [videos, setVideos] = useState<Video[] | null>(null)
  const [libs, setLibs] = useState<Library[]>([])
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [downloading, setDownloading] = useState(false)
  const [langs, setLangs] = useState<string[]>(['zh', 'en'])

  const status = (params.get('status') as StatusFilter) || 'all'
  const libraryId = params.get('library') || ''
  const focusVideo = Number(params.get('video')) || null

  const load = useCallback(async () => {
    try {
      const query = new URLSearchParams({ limit: '1000' })
      if (libraryId) query.set('library_id', libraryId)
      const [vRes, lRes, cRes] = await Promise.all([
        api.get<{ items: Video[] }>(`/api/videos?${query}`),
        api.get<{ items: Library[] }>('/api/libraries'),
        api.get<{ items: { key: string; value: unknown }[] }>('/api/config'),
      ])
      setVideos(vRes.items); setLibs(lRes.items)
      const li = cRes.items.find((i) => i.key === 'general.languages')
      if (Array.isArray(li?.value)) setLangs(li.value as string[])
      setError('')
    } catch (e) { setError((e as Error).message) }
  }, [libraryId])
  useEffect(() => { load() }, [load])

  const filtered = useMemo(() => {
    if (!videos) return []
    return videos.filter((v) => {
      if (status === 'missing') return isMissing(v)
      if (status === 'has_subtitle') return v.status === 'has_subtitle'
      if (status === 'needs_attention') return v.status === 'needs_attention' || v.status === 'failed'
      return true
    })
  }, [videos, status])

  function setFilter(next: StatusFilter) {
    const p = new URLSearchParams(params)
    if (next === 'all') p.delete('status'); else p.set('status', next)
    setParams(p)
  }

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  async function batchDownload() {
    if (!selected.size) return
    setDownloading(true)
    try {
      const r = await api.post<{ job_id: number }>('/api/jobs/download', { video_ids: [...selected] })
      push(`下载任务 #${r.job_id} 已创建`)
      setSelected(new Set())
      navigate(`/jobs?job=${r.job_id}`)
    } catch (e) { push((e as Error).message, 'error') }
    finally { setDownloading(false) }
  }

  if (videos === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>剧集</h1>
          <p className="page-sub">共 {videos.length} 集，筛选缺口并批量下载</p>
        </div>
        <button className="btn btn-primary" onClick={batchDownload} disabled={!selected.size || downloading}>
          {downloading ? '提交中…' : `↓ 下载选中 (${selected.size})`}
        </button>
      </div>

      <Card>
        <div className="filter-bar">
          <div className="segmented">
            {([['all', '全部'], ['missing', '仅缺字幕'], ['has_subtitle', '已有字幕'], ['needs_attention', '需关注']] as [StatusFilter, string][]).map(([key, label]) => (
              <button key={key} className={status === key ? 'active' : ''} onClick={() => setFilter(key)}>{label}</button>
            ))}
          </div>
          <select className="select" value={libraryId} onChange={(e) => {
            const p = new URLSearchParams(params)
            if (e.target.value) p.set('library', e.target.value); else p.delete('library')
            setParams(p)
          }}>
            <option value="">所有媒体库</option>
            {libs.map((l) => <option key={l.id} value={l.id}>{l.name || l.root_path}</option>)}
          </select>
          <label className="inline-check">
            <input type="checkbox" checked={selected.size === filtered.length && filtered.length > 0}
              onChange={(e) => setSelected(e.target.checked ? new Set(filtered.map((v) => v.id)) : new Set())} />
            全选
          </label>
        </div>

        {filtered.length === 0 ? <Empty text="没有符合条件的剧集" hint="先在媒体库触发一次扫描" /> : (
          <div className="video-table">
            <div className="video-row video-row-head">
              <span className="col-check" />
              <span className="col-title">剧集</span>
              <span className="col-status">状态</span>
              <span className="col-langs">字幕语言</span>
              <span className="col-actions">操作</span>
            </div>
            {filtered.map((v) => (
              <VideoRow key={v.id} video={v} langs={langs} selected={selected.has(v.id)}
                focused={focusVideo === v.id} onToggle={() => toggle(v.id)} push={push} onChanged={load} />
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}

function VideoRow({ video, langs, selected, focused, onToggle, push, onChanged }: {
  video: Video; langs: string[]; selected: boolean; focused: boolean
  onToggle: () => void; push: ToastPush; onChanged: () => void
}) {
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (focused) document.getElementById(`video-${video.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [focused, video.id])

  async function downloadOne() {
    setBusy(true)
    try {
      const r = await api.post<{ job_id: number }>('/api/jobs/download', { video_ids: [video.id] })
      push(`下载任务 #${r.job_id} 已创建`)
      onChanged()
    } catch (e) { push((e as Error).message, 'error') }
    finally { setBusy(false) }
  }

  const missingLangs = langs.filter((l) => !video.subtitles.some((s) => s.language === l || s.language.startsWith(l)))
  const statusTone = video.status === 'has_subtitle' ? 'ok' : isMissing(video) ? 'warn' : 'muted'

  return (
    <div id={`video-${video.id}`} className={`video-row ${focused ? 'focused' : ''}`}>
      <span className="col-check"><input type="checkbox" checked={selected} onChange={onToggle} /></span>
      <span className="col-title">
        <span className="video-title-main">{video.title || fileName(video.path)} {video.episode ? `· E${video.episode}` : ''}</span>
        <span className="video-path">{fileName(video.path)}</span>
      </span>
      <span className="col-status"><Badge tone={statusTone}>{statusLabel(video.status)}</Badge></span>
      <span className="col-langs">
        {langs.map((l) => {
          const sub = video.subtitles.find((s) => s.language === l || s.language.startsWith(l))
          return sub ? (
            <Badge key={l} tone={sub.is_manual ? 'accent' : 'ok'}>{langLabel(l)}{sub.is_manual ? '·人工' : ''}</Badge>
          ) : <Badge key={l} tone="empty">{langLabel(l)}·缺</Badge>
        })}
      </span>
      <span className="col-actions">
        {(missingLangs.length > 0 || isMissing(video)) && (
          <button className="btn btn-sm" onClick={downloadOne} disabled={busy}>{busy ? '…' : '↓ 下载'}</button>
        )}
      </span>
    </div>
  )
}

function isMissing(v: Video): boolean {
  return ['missing', 'pending', 'needs_attention', 'failed'].includes(v.status)
}
function statusLabel(s: string): string {
  const map: Record<string, string> = {
    has_subtitle: '已有字幕', missing: '缺字幕', pending: '待处理', needs_attention: '需人工',
    failed: '失败', skipped: '已跳过', scanned: '已扫描',
  }
  return map[s] || s
}
function fileName(path: string): string {
  const parts = path.replace(/\\/g, '/').split('/')
  return parts[parts.length - 1] || path
}
