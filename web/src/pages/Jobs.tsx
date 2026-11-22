import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import type { Job, Decision } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, humanizeReason, langLabel, stateTone, timeAgo, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']
const MAX_DECISIONS = 200

export default function Jobs({ push }: { push: ToastPush }) {
  const [params] = useSearchParams()
  const focusJob = Number(params.get('job')) || null
  const [jobs, setJobs] = useState<Job[] | null>(null)
  const [error, setError] = useState('')
  const [activeId, setActiveId] = useState<number | null>(focusJob)
  const [decisions, setDecisions] = useState<Decision[]>([])
  const [live, setLive] = useState<{ state: string; progress: number; error_message: string | null } | null>(null)
  const closeStream = useRef<(() => void) | null>(null)

  const load = useCallback(async () => {
    try {
      // 优先用列表端点；不可用时回退到并行回溯，兼容旧后端。
      try {
        const r = await api.get<{ items: Job[] }>('/api/jobs?limit=30')
        setJobs(r.items); setError(''); return
      } catch { /* 回退 */ }
      const results = await Promise.allSettled(
        Array.from({ length: 120 }, (_, i) => api.get<Job>(`/api/jobs/${i + 1}`)),
      )
      const found = results
        .filter((r): r is PromiseFulfilledResult<Job> => r.status === 'fulfilled')
        .map((r) => r.value)
        .slice(-30)
      setJobs(found.reverse()); setError('')
    } catch (e) { setError((e as Error).message) }
  }, [])
  useEffect(() => { load() }, [load])

  const loadDecisions = useCallback(async (id: number) => {
    try {
      const r = await api.get<{ items: Decision[] }>(`/api/jobs/${id}/decisions`)
      setDecisions(r.items.slice(-MAX_DECISIONS))
    } catch { /* 日志加载失败不影响进度展示 */ }
  }, [])

  function selectJob(id: number) {
    setActiveId(id); setDecisions([]); setLive(null)
    if (closeStream.current) closeStream.current()
    loadDecisions(id)
    closeStream.current = api.streamJob(id, (payload, type) => {
      if (type === 'job.decisions' && payload.items) {
        const incoming = payload.items
        setDecisions((prev) => {
          const seen = new Set(prev.map((d) => d.id))
          return [...prev, ...incoming.filter((d) => !seen.has(d.id))].slice(-MAX_DECISIONS)
        })
        return
      }
      setLive((prev) => ({
        state: payload.state ?? prev?.state ?? '',
        progress: payload.progress ?? prev?.progress ?? 0,
        error_message: payload.error_message ?? prev?.error_message ?? null,
      }))
      if (type === 'job.done') {
        load(); loadDecisions(id)
        push(`任务 #${id} 已结束：${payload.state}`, payload.state === 'FAILED' ? 'error' : 'ok')
      }
    }, () => { /* 流结束 */ })
  }

  useEffect(() => {
    if (focusJob && jobs) selectJob(focusJob)
    return () => { if (closeStream.current) closeStream.current() }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusJob, jobs !== null])

  if (jobs === null) return error ? <ErrorBox message={error} onRetry={load} /> : <Spinner />

  const active = jobs.find((j) => j.id === activeId) || null
  const shownState = live?.state || active?.state
  const shownProgress = live?.progress ?? active?.progress ?? 0
  const scanFiles = decisions.filter((d) => d.step === 'scan_file')
  const downloadResults = decisions.filter((d) => d.step === 'download_result')
  const otherDecisions = decisions.filter((d) => d.step !== 'scan_file' && d.step !== 'download_result')
  const lastScanMeta = scanFiles.length ? scanMeta(scanFiles[scanFiles.length - 1]) : {}
  const scanTotal = lastScanMeta.total || decisions.find((d) => d.step === 'scan' && d.reason?.includes('共发现'))?.reason?.match(/\d+/)?.[0] || '?'
  // 兼容早期任务：统计值曾写进 error_message，这里转成友好摘要，不再显示成红色报错。
  const legacyMatch = active?.error_message?.match(/downloaded=(\d+), failed=(\d+)/)
  const legacySummary = legacyMatch ? `下载完成：成功 ${legacyMatch[1]} 个，失败 ${legacyMatch[2]} 个` : ''

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>任务中心</h1>
          <p className="page-sub">实时进度、失败原因与逐步决策日志</p>
        </div>
        <button className="btn" onClick={load}>刷新列表</button>
      </div>

      <div className="grid-jobs">
        <Card title="近期任务" subtitle={`${jobs.length} 个`}>
          {jobs.length === 0 ? <Empty text="还没有任务" hint="扫描媒体库或下载字幕后在此显示" /> : (
            <div className="job-list">
              {jobs.map((j) => (
                <button key={j.id} className={`job-item ${activeId === j.id ? 'active' : ''}`} onClick={() => selectJob(j.id)}>
                  <div className="job-item-head">
                    <span className="job-id">#{j.id}</span>
                    <Badge tone={stateTone(j.state)}>{j.state}</Badge>
                  </div>
                  <div className="job-item-meta">{j.type === 'download_batch' ? '批量下载' : '扫描媒体库'} · {timeAgo(j.created_at)}</div>
                  <div className="job-progress-track"><div className="job-progress-fill" style={{ width: `${j.progress}%` }} /></div>
                </button>
              ))}
            </div>
          )}
        </Card>

        <Card title={active ? `任务 #${active.id} 详情` : '任务详情'} subtitle={active ? `${active.type} · ${timeAgo(active.created_at)}` : undefined}>
          {!active ? <Empty text="选择一个任务查看详情" /> : (
            <>
              <div className="job-detail-head">
                <Badge tone={stateTone(shownState || '')}>{shownState}</Badge>
                <span className="job-progress-num">{shownProgress}%</span>
              </div>
              <div className="job-progress-track big"><div className="job-progress-fill" style={{ width: `${shownProgress}%` }} /></div>
              {shownState === 'RUNNING' && <p className="live-hint"><span className="dot dot-ok dot-pulse" /> 实时更新中（SSE）</p>}
              {shownState !== 'SUCCESS' && (active.error_message || live?.error_message) && (
                <div className="error-box"><span>{humanizeReason(live?.error_message || active.error_message)}</span></div>
              )}
              <h3 className="subhead">决策日志</h3>
              {decisions.length === 0 && !legacySummary ? <Empty text="暂无决策记录" hint="扫描或下载时这里会实时列出正在处理的媒体文件" /> : (
                <>
                  {legacySummary && downloadResults.length === 0 && (
                    <div className="download-legacy">
                      <span>{legacySummary}</span>
                      <small>该任务创建于逐文件详情记录之前，暂无单个文件明细</small>
                    </div>
                  )}
                  {scanFiles.length > 0 && (
                    <div className="scan-file-block">
                      <p className="scan-file-head">扫描文件 · {scanFiles.length}/{scanTotal}</p>
                      <div className="scan-file-list">
                        {scanFiles.map((d, idx) => {
                          const meta = scanMeta(d)
                          const path = meta.path || d.reason || ''
                          const { name, dir } = splitPath(path)
                          const isCurrent = shownState === 'RUNNING' && idx === scanFiles.length - 1
                          return (
                            <div key={d.id} className={`scan-file-row ${isCurrent ? 'current' : ''}`}>
                              <span className="scan-index">{meta.index ?? idx + 1}/{meta.total ?? scanTotal}</span>
                              <span className="scan-file-name" title={path}>{name || path}</span>
                              <span className="scan-file-dir" title={dir}>{dir || '—'}</span>
                              {isCurrent && <Badge tone="warn">解析中</Badge>}
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}
                  {downloadResults.length > 0 && (
                    <div className="download-result-block">
                      <p className="download-result-head">下载详情 · {downloadResults.length} 条</p>
                      <div className="download-result-list">
                        {downloadResults.map((d) => {
                          const meta = downloadMeta(d)
                          const { name } = splitPath(meta.video_path || d.reason || '')
                          return (
                            <div key={d.id} className="download-result-row">
                              <Badge tone="default">{langLabel(meta.language || '')}</Badge>
                              <Badge tone={downloadTone(meta.status)}>{downloadLabel(meta.status)}</Badge>
                              {meta.score != null && <Badge tone={meta.score >= 60 ? 'ok' : 'warn'}>{meta.score}分</Badge>}
                              <span className="download-video" title={meta.video_path}>{name || '—'}</span>
                              <span className="download-detail" title={d.reason || meta.detail || ''}>
                                {d.reason || meta.detail || '—'}
                                {meta.provider && <span className="download-provider"> · {meta.provider}</span>}
                              </span>
                            </div>
                          )
                        })}
                      </div>
                    </div>
                  )}
                  {otherDecisions.length > 0 && (
                    <div className="decision-list">
                      {otherDecisions.map((d) => (
                        <div key={d.id} className="decision-row">
                          <span className={`decision-step step-${d.step}`}>{stepLabel(d.step)}</span>
                          <span className="decision-provider">{d.provider || '—'}</span>
                          {d.score != null && <Badge tone={d.score >= 60 ? 'ok' : 'warn'}>{d.score}分</Badge>}
                          <span className="decision-reason">{humanizeReason(d.reason)}</span>
                          {d.latency_ms != null && <span className="decision-latency">{d.latency_ms}ms</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </Card>
      </div>
    </div>
  )
}

function scanMeta(d: Decision | undefined): { index?: number; total?: number; path?: string } {
  if (!d || !d.candidates || typeof d.candidates !== 'object' || Array.isArray(d.candidates)) return {}
  return d.candidates as { index?: number; total?: number; path?: string }
}

interface DownloadMeta {
  video_path?: string
  language?: string
  status?: string
  provider?: string | null
  score?: number | null
  subtitle_path?: string | null
  detail?: string | null
}

function downloadMeta(d: Decision): DownloadMeta {
  if (!d.candidates || typeof d.candidates !== 'object' || Array.isArray(d.candidates)) return {}
  return d.candidates as DownloadMeta
}

function downloadLabel(status?: string): string {
  const map: Record<string, string> = {
    downloaded: '已下载', skipped_existing: '已跳过', no_candidate: '无候选',
    below_threshold: '评分不足', throttled: '限流', failed: '失败',
  }
  return status ? (map[status] || status) : '未知'
}

function downloadTone(status?: string): string {
  if (status === 'downloaded') return 'ok'
  if (status === 'failed' || status === 'throttled') return 'error'
  if (status === 'below_threshold' || status === 'no_candidate') return 'warn'
  return 'muted'
}

// 兼容 Windows 反斜杠与 POSIX 斜杠路径。
function splitPath(path: string): { name: string; dir: string } {
  const i = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))
  return i >= 0 ? { name: path.slice(i + 1), dir: path.slice(0, i) } : { name: path, dir: '' }
}

function stepLabel(step: string): string {
  const map: Record<string, string> = {
    scan: '扫描完成', scan_file: '扫描文件', download_result: '下载详情', download_summary: '下载汇总',
    refine: '元数据', search: '搜索', searched: '搜索完成', scored: '评分',
    downloaded: '已下载', below_threshold: '评分不足', no_candidate: '无候选',
    save_failed: '落盘失败', provider_throttled: '源熔断', skip_existing: '跳过已有',
  }
  return map[step] || step
}
