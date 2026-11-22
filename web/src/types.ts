// 与后端 API 对齐的类型定义。

export interface UserInfo {
  id: number
  username: string
}

export interface ConfigItem {
  key: string
  value: unknown
  value_type: 'str' | 'int' | 'bool' | 'json'
  sensitive: boolean
  description: string
  masked: boolean
}

export interface ConfigResponse {
  version: number
  items: ConfigItem[]
}

export interface ConfigAuditItem {
  id: number
  key: string
  old_value: unknown
  new_value: unknown
  summary?: string
  operator: string
  changed_at: string
}

export interface Library {
  id: number
  root_path: string
  name: string
  scan_cron: string
  enabled: boolean
}

export interface SubtitleInfo {
  language: string
  provider: string
  score: number
  path: string
  is_manual: boolean
}

export interface Video {
  id: number
  library_id: number
  path: string
  title: string | null
  year: number | null
  season: number | null
  episode: number | null
  status: string
  fps: number | null
  embedded_langs: string | null
  subtitles: SubtitleInfo[]
}

export interface Job {
  id: number
  type: string
  state: 'PENDING' | 'RUNNING' | 'SUCCESS' | 'PARTIAL' | 'FAILED' | 'SKIPPED'
  progress: number
  error_code: string | null
  error_message: string | null
  created_at: string | null
  finished_at: string | null
}

export interface Decision {
  id: number
  step: string
  provider: string | null
  score: number | null
  reason: string | null
  candidates: unknown
  latency_ms: number | null
  created_at: string | null
}

export interface Candidate {
  id: number
  video_id: number
  language: string
  provider: string
  external_id: string
  release_name: string
  fps: number | null
  hearing_impaired: boolean | null
  score: number
  score_breakdown: Record<string, number>
  extra: Record<string, unknown>
  subtype: string | null
  lang_desc: string | null
  status: string
  created_at: string | null
}

export interface ProviderItem {
  name: string
  display_name: string
  kind: 'custom' | 'native'
  languages: string[]
  enabled: boolean
  priority: number
  state: string
  fail_count: number
  cooldown_until: string | null
  rate_limit: unknown
  credential_configured: boolean | null
}

export interface HealthCheck {
  name: string
  status: 'ok' | 'warn' | 'fail'
  detail: string
  fix: string
}

export interface HealthReport {
  ok: boolean
  summary: { ok: number; warn: number; fail: number }
  checks: HealthCheck[]
}
