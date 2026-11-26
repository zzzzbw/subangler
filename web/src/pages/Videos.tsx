import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { api } from '../api'
import type { Video, Library } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, langLabel, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

type StatusFilter = 'all' | 'missing' | 'has_subtitle' | 'needs_attention'
const PAGE_SIZES = [20, 50, 100, 200]

export default function Videos({ push }: { push: ToastPush }) {
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const [videos, setVideos] = useState<Video[] | null>(null)
  const [libs, setLibs] = useState<Library[]>([])
  const [total, setTotal] = useState(0)
  const [pages, setPages] = useState(0)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [downloading, setDownloading] = useState(false)
  const [langs, setLangs] = useState<string[]>(['zh', 'en'])

  const status = (params.get('status') as StatusFilter) || 'all'
  const libraryId = params.get('library') || ''
  const qParam = params.get('q') || ''
  const page = Math.max(1, Number(params.get('page')) || 1)
  const pageSize = PAGE_SIZES.includes(Number(params.get('size'))) ? Number(params.get('size')) : 50
  const focusVideo = Number(params.get('video')) || null

  // 搜索框本地值 + 防抖写回 URL，避免每敲一个字就请求一次。
  const [searchInput, setSearchInput] = useState(qParam)
  const paramsRef = useRef(params)
  paramsRef.current = params
  const qParamRef = useRef(qParam)
  qParamRef.current = qParam
  useEffect(() => { setSearchInput(qParam) }, [qParam])
  useEffect(() => {
    if (searchInput === qParamRef.current) return
    const timer = setTimeout(() => {
      const p = new URLSearchParams(paramsRef.current)
      const value = searchInput.trim()
      if (value) p.set('q', value); else p.delete('q')
      p.delete('page') // 搜索条件变化回到第一页
      setParams(p)
    }, 300)
    return () => clearTimeout(timer)
  }, [searchInput, setParams])
  // 数据变化后页码越界则回到最后一页。
  useEffect(() => {
    if (pages > 0 && page > pages) {
      const p = new URLSearchParams(params)
      p.set('page', String(pages))
      setParams(p)
    }
  }, [pages, page, params, setParams])

  const load = useCallback(async () => {
    try {
      const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
      if (libraryId) query.set('library_id', libraryId)
      if (status !== 'all') query.set('status', status)
      if (qParam) query.set('q', qParam)
      const [vRes, lRes, cRes] = await Promise.all([
        api.get<{ items: Video[]; total: number; pages: number }>(`/api/videos?${query}`),
        api.get<{ items: Library[] }>('/api/libraries'),
        api.get<{ items: { key: string; value: unknown }[] }>('/api/config'),
      ])
      setVideos(vRes.items); setTotal(vRes.total); setPages(vRes.pages); setLibs(lRes.items)
      const li = cRes.items.find((i) => i.key === 'general.languages')
      if (Array.isArray(li?.value)) setLangs(li.value as string[])
      setError('')
    } catch (e) { setError((e as Error).message) }
  }, [libraryId, status, qParam, page, pageSize])
  useEffect(() => { load() }, [load])

  function updateParam(key: string, value: string, resetPage = true) {
    const p = new URLSearchParams(params)
    if (value) p.set(key, value); else p.delete(key)
    if (resetPage) p.delete('page')
    setParams(p)
  }

  function goPage(next: number) {
    const p = new URLSearchParams(params)
    p.set('page', String(Math.max(1, Math.min(next, pages || 1))))
    setParams(p)
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

  const pageIds = videos.map((v) => v.id)
  const allPageSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))
  const focusOnPage = focusVideo != null && pageIds.includes(focusVideo)

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>剧集</h1>
          <p className="page-sub">共 {total} 集，筛选缺口并批量下载</p>
        </div>
        <button className="btn btn-primary" onClick={batchDownload} disabled={!selected.size || downloading}>
          {downloading ? '提交中…' : `↓ 下载选中 (${selected.size})`}
        </button>
      </div>

      <Card>
        <div className="filter-bar">
          <div className="segmented">
            {([['all', '全部'], ['missing', '仅缺字幕'], ['has_subtitle', '已有字幕'], ['needs_attention', '需关注']] as [StatusFilter, string][]).map(([key, label]) => (
              <button key={key} className={status === key ? 'active' : ''}
                onClick={() => updateParam('status', key === 'all' ? '' : key)}>{label}</button>
            ))}
          </div>
          <select className="select" value={libraryId} onChange={(e) => updateParam('library', e.target.value)}>
            <option value="">所有媒体库</option>
            {libs.map((l) => <option key={l.id} value={l.id}>{l.name || l.root_path}</option>)}
          </select>
          <input className="search-input" type="search" placeholder="搜索剧名或路径…" value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)} />
          <label className="inline-check">
            <input type="checkbox" checked={allPageSelected} onChange={(e) => setSelected((prev) => {
              const next = new Set(prev)
              for (const id of pageIds) { if (e.target.checked) next.add(id); else next.delete(id) }
              return next
            })} />
            全选本页
          </label>
        </div>

        {error && <ErrorBox message={error} onRetry={load} />}
        {focusVideo != null && !focusOnPage && (
          <div className="field-hint">跳转的剧集不在当前页，可用搜索框定位。</div>
        )}

        {videos.length === 0 ? <Empty text="没有符合条件的剧集" hint="试试调整搜索词或筛选条件，或先在媒体库触发一次扫描" /> : (
          <div className="video-table">
            <div className="video-row video-row-head">
              <span className="col-check" />
              <span className="col-title">剧集</span>
              <span className="col-status">状态</span>
              <span className="col-langs">字幕语言</span>
              <span className="col-actions">操作</span>
            </div>
            {videos.map((v) => (
              <VideoRow key={v.id} video={v} langs={langs} selected={selected.has(v.id)}
                focused={focusVideo === v.id} onToggle={() => toggle(v.id)} push={push} onChanged={load} />
            ))}
          </div>
        )}

        <div className="pager">
          <span className="pager-info">第 {page} / {Math.max(pages, 1)} 页 · 共 {total} 集</span>
          <div className="pager-controls">
            <button className="btn btn-sm" disabled={page <= 1} onClick={() => goPage(page - 1)}>上一页</button>
            <button className="btn btn-sm" disabled={page >= Math.max(pages, 1)} onClick={() => goPage(page + 1)}>下一页</button>
            <select className="select" value={pageSize} onChange={(e) => updateParam('size', e.target.value)}>
              {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} 条/页</option>)}
            </select>
          </div>
        </div>
      </Card>
    </div>
  )

  function toggle(id: number) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }
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
