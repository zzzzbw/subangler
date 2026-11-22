import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import type { Job, Decision } from '../types'
import { Badge, Card, Empty, ErrorBox, Spinner, humanizeReason, stateTone, timeAgo, useToasts } from '../components/ui'

type ToastPush = ReturnType<typeof useToasts>['push']

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

  function selectJob(id: number) {
    setActiveId(id); setDecisions([]); setLive(null)
    if (closeStream.current) closeStream.current()
    api.get<{ items: Decision[] }>(`/api/jobs/${id}/decisions`).then((r) => setDecisions(r.items)).catch(() => {})
    closeStream.current = api.streamJob(id, (payload, type) => {
      setLive(payload)
      if (type === 'job.done') { load(); push(`任务 #${id} 已结束：${payload.state}`, payload.state === 'FAILED' ? 'error' : 'ok') }
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
              {(active.error_message || live?.error_message) && (
                <div className="error-box"><span>{humanizeReason(live?.error_message || active.error_message)}</span></div>
              )}
              <h3 className="subhead">决策日志</h3>
              {decisions.length === 0 ? <Empty text="暂无决策记录" /> : (
                <div className="decision-list">
                  {decisions.map((d, idx) => (
                    <div key={idx} className="decision-row">
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
        </Card>
      </div>
    </div>
  )
}

function stepLabel(step: string): string {
  const map: Record<string, string> = {
    scan: '扫描', refine: '元数据', search: '搜索', searched: '搜索完成', scored: '评分',
    downloaded: '已下载', below_threshold: '评分不足', no_candidate: '无候选',
    save_failed: '落盘失败', provider_throttled: '源熔断', skip_existing: '跳过已有',
  }
  return map[step] || step
}
