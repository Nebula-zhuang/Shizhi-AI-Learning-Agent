/**
 * Tutor 教学闭环接口封装（P4）+ 学习看板与讲法画像（P5）。
 *
 * 对应后端 app/api/routes/tutor.py。
 */

import { apiUrl, extractError, getJson, postJson, putJson } from './http'

// --------------------------------------------------------------------------- #
// 类型
// --------------------------------------------------------------------------- #
export type TutorAction =
  | 'probe'
  | 'explain'
  | 'rephrase'
  | 'harder'
  | 'easier'
  | 'summarize'

export interface TutorSource {
  document_id: number | null
  file_name: string
  chunk_index: number | null
  page_label: string
  page_start: number | null
  heading_path: string[]
  distance: number
  content: string
}

export interface TutorAssessment {
  correct: boolean
  score: number
  /** 评估本身的把握，**不是掌握度** */
  confidence: number
  level: string
  error_type: string
  missing_points: string[]
  misunderstood_points: string[]
  feedback: string
  engine: string
}

export interface TutorLearnerState {
  learner_id: string
  knowledge_point_id: number | null
  exists: boolean
  /** 掌握度 0~1 —— 这才是学习状态 */
  mastery: number
  attempt_count: number
  correct_count: number
  consecutive_correct: number
  consecutive_wrong: number
  status: string
  last_error_type: string | null
  last_attempt_at: string | null
  updated_at: string | null
  accuracy: number
}

export interface PolicyDecision {
  action: string
  reason: string
  rule: string
  forced: boolean
  allowed: string[]
  confidence: number
}

export interface ProposalResult {
  ok: boolean
  action: string
  reason: string
  confidence: number
  reject_reason: string
  detail: string
}

export interface TutorTurn {
  session_id: number
  knowledge_point_id: number
  title: string
  action: TutorAction
  content: string
  reason: string
  trace: string[]
  decision: PolicyDecision
  proposal: ProposalResult | null
  assessment: TutorAssessment | null
  state_before: TutorLearnerState
  state_after: TutorLearnerState
  state_updated: boolean
  sources: TutorSource[]
  tools: Record<string, unknown>
  degraded: boolean
  notes: string[]
  /** P5：本轮用到的长期记忆（讲法偏好 / 是否到期 / 主动回顾提示） */
  memory: TutorMemory
  session_finished: boolean
  thresholds: Record<string, number>
}

export interface TutorSessionDetail {
  session_id: number
  learner_id: string
  knowledge_point_id: number | null
  title: string
  status: string
  action_count: number
  action_sequence: string[]
  state: TutorLearnerState | null
  messages: {
    id: number
    role: string
    content: string
    action_type: string | null
    reason: string
    state_snapshot: Record<string, unknown> | null
    refs: TutorSource[] | null
    engine: string
    created_at: string
  }[]
  evaluations: {
    id: number
    correct: boolean
    score: number
    confidence: number
    level: string
    error_type: string
    missing_points: string[]
    misunderstood_points: string[]
    feedback: string
    engine: string
  }[]
}

export interface TutorCapabilities {
  actions: TutorAction[]
  action_labels: Record<TutorAction, string>
  thresholds: Record<string, number>
  max_tool_calls: number
  max_seconds: number
  llm_mode: string
  llm_model: string
}

// --------------------------------------------------------------------------- #
// 接口
// --------------------------------------------------------------------------- #
export function getTutorCapabilities(): Promise<TutorCapabilities> {
  return getJson<TutorCapabilities>('/api/tutor/capabilities')
}

export function startTutor(knowledgePointId: number): Promise<TutorTurn> {
  return postJson<TutorTurn>('/api/tutor/start', { knowledge_point_id: knowledgePointId })
}

export function answerTutor(sessionId: number, answer: string): Promise<TutorTurn> {
  return postJson<TutorTurn>('/api/tutor/answer', { session_id: sessionId, answer })
}

// --------------------------------------------------------------------------- #
// 流式版本：阶段反馈 + 正文增量
// --------------------------------------------------------------------------- #

export interface TutorStreamHandlers {
  /** 后台现在在干嘛（来自运行时状态机，不是假进度） */
  onStage?: (label: string) => void
  /** 正文的文字增量，边生成边到 */
  onContent?: (text: string) => void
  signal?: AbortSignal
}

/**
 * 解析一个教学回合的 SSE 流。
 *
 * 为什么不用 `EventSource`：它只支持 GET，而提交作答必须 POST。
 * 所以用 `fetch` 的 ReadableStream 手工解 SSE 帧 —— 帧格式很简单
 * （`event: xxx` / `data: json` / 空行分隔），不值得引库。
 *
 * 四种帧：stage → onStage；content → onContent；done → 返回完整结果；error → 抛错。
 * `done` 的载荷与同步接口的响应体**完全一致**，所以两种路径可以互换。
 */
async function streamTurn(
  path: string,
  body: unknown,
  handlers: TutorStreamHandlers = {},
): Promise<TutorTurn> {
  let response: Response
  try {
    response = await fetch(apiUrl(path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      credentials: 'include',
      body: JSON.stringify(body),
      signal: handlers.signal,
    })
  } catch {
    throw new Error('连不上服务。请确认学习伙伴还在运行，稍后再试。')
  }

  if (!response.ok) throw new Error(await extractError(response))
  if (!response.body) throw new Error('服务没有返回流式响应。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let result: TutorTurn | null = null
  let failure: string | null = null

  const consume = (frame: string) => {
    let event = 'message'
    const dataLines: string[] = []
    for (const line of frame.split(/\r?\n/)) {
      if (line.startsWith('event: ')) event = line.slice(7).trim()
      else if (line.startsWith('data: ')) dataLines.push(line.slice(6))
    }
    if (dataLines.length === 0) return
    let payload: Record<string, unknown>
    try {
      payload = JSON.parse(dataLines.join('\n')) as Record<string, unknown>
    } catch {
      return
    }
    if (event === 'stage' && typeof payload.label === 'string') {
      handlers.onStage?.(payload.label)
    } else if (event === 'content' && typeof payload.text === 'string') {
      handlers.onContent?.(payload.text)
    } else if (event === 'done') {
      result = payload as unknown as TutorTurn
    } else if (event === 'error') {
      failure = typeof payload.message === 'string' ? payload.message : '这一轮没能完成。'
    }
  }

  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // 以空行分帧；最后一段可能是半帧，留在 buffer 里等下一块
    const frames = buffer.split(/\r?\n\r?\n/)
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      if (frame.trim()) consume(frame)
    }
  }
  if (buffer.trim()) consume(buffer)

  if (failure) throw new Error(failure)
  if (!result) throw new Error('连接提前结束，这一轮没有完成。')
  return result
}

export function startTutorStream(
  knowledgePointId: number,
  handlers?: TutorStreamHandlers,
): Promise<TutorTurn> {
  return streamTurn(
    '/api/tutor/start/stream',
    { knowledge_point_id: knowledgePointId },
    handlers,
  )
}

export function answerTutorStream(
  sessionId: number,
  answer: string,
  handlers?: TutorStreamHandlers,
): Promise<TutorTurn> {
  return streamTurn(
    '/api/tutor/answer/stream',
    { session_id: sessionId, answer },
    handlers,
  )
}

export function getTutorSession(sessionId: number): Promise<TutorSessionDetail> {
  return getJson<TutorSessionDetail>(`/api/tutor/sessions/${sessionId}`)
}

export function getLearnerState(knowledgePointId: number): Promise<TutorLearnerState> {
  return getJson<TutorLearnerState>(`/api/tutor/learner-state/${knowledgePointId}`)
}

// --------------------------------------------------------------------------- #
// 展示文案
// --------------------------------------------------------------------------- #
export const ACTION_LABEL: Record<string, string> = {
  probe: '追问',
  explain: '讲解',
  rephrase: '换讲法',
  harder: '升难度',
  easier: '降难度',
  summarize: '总结',
}

export const STATUS_LABEL: Record<string, string> = {
  new: '未开始',
  learning: '学习中',
  weak: '薄弱',
  mastered: '已掌握',
}

/**
 * 错因展示名。
 *
 * **措辞必须与后端 `app/services/memory_service.py` 的 `ERROR_LABEL` 一致** ——
 * 同一个错因在回顾提示里叫"记不牢"、在状态标签里叫"记忆缺口"，
 * 用户会以为是两回事。跨语言没法用测试钉住，改一处要同时改另一处。
 */
export const ERROR_TYPE_LABEL: Record<string, string> = {
  concept_confusion: '概念混淆',
  memory_gap: '记忆缺口',
  reasoning_break: '推理断裂',
  misread: '看错题意',
  none: '理解不到位',
}

export const LEVEL_LABEL: Record<string, string> = {
  not_mastered: '没答到点上',
  vague: '答得不够透',
  mastered: '答得完整',
}

// --------------------------------------------------------------------------- #
// P5：学习看板与讲法画像
// --------------------------------------------------------------------------- #
export interface MasteryOverview {
  knowledge_point_total: number
  tracked: number
  /** 从未作答过的知识点数 */
  untouched: number
  new: number
  learning: number
  weak: number
  mastered: number
}

export interface WeakPoint {
  knowledge_point_id: number
  title: string
  mastery: number
  status: string
  attempt_count: number
  consecutive_wrong: number
  importance: number
  last_error_type: string | null
  next_review_at: string | null
  /** 紧迫度，越高越该先看 */
  urgency: number
  /** 是否已到复习时间 */
  due: boolean
}

export interface LearnerProfileSummary {
  preferred_style: string
  style_label: string
  style_source: 'derived' | 'manual' | 'default'
  /** 与注入 Prompt 的**逐字一致** —— 前后端不为两套说法 */
  style_instruction: string
  style_evidence: Record<string, unknown>
}

export interface ReviewCurve {
  tiers: { mastery_below: number; interval_seconds: number }[]
  wrong_factor: number
  wrong_streak_threshold: number
  min_seconds: number
}

export interface TutorDashboard {
  learner_id: string
  generated_at: string
  overview: MasteryOverview
  profile: LearnerProfileSummary
  due_reviews: WeakPoint[]
  weak_points: WeakPoint[]
  review_curve: ReviewCurve
}

/** 一轮教学里用到的长期记忆（P5）。 */
export interface TutorMemory {
  learner_id: string
  preferred_style: string
  style_label: string
  style_source: string
  style_instruction: string
  knowledge_state: Record<string, unknown> | null
  is_due: boolean
  last_error_type: string | null
  other_weak_points: Record<string, unknown>[]
  should_recall: boolean
  recall_note: string
  /** 本轮是否真的做了主动回顾 */
  recalled: boolean
}

export function getTutorDashboard(learnerId?: string): Promise<TutorDashboard> {
  const query = learnerId ? `?learner_id=${encodeURIComponent(learnerId)}` : ''
  return getJson<TutorDashboard>(`/api/tutor/dashboard${query}`)
}

export function updateLearnerProfile(
  preferredStyle: string,
  learnerId?: string,
): Promise<LearnerProfileSummary> {
  return putJson<LearnerProfileSummary>('/api/tutor/profile', {
    preferred_style: preferredStyle,
    learner_id: learnerId ?? null,
  })
}

export const STYLE_LABEL: Record<string, string> = {
  balanced: '均衡讲法',
  contrast: '对比式讲法',
  structured: '结构化讲法',
  stepwise: '分步式讲法',
  clarify: '审题式讲法',
}

export const STYLE_SOURCE_LABEL: Record<string, string> = {
  derived: '系统根据你的错因推导',
  manual: '你手动设定',
  default: '还没有足够依据',
}

/** 所有可选讲法，供下拉框用。顺序固定，避免每次渲染顺序不同。 */
export const ALL_STYLES: string[] = [
  'balanced',
  'contrast',
  'structured',
  'stepwise',
  'clarify',
]

/** 把秒数压成好读的中文间隔 */
export function humanizeSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时`
  return `${Math.round(seconds / 86400)} 天`
}
