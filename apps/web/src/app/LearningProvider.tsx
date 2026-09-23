/**
 * 学习上下文 —— 整个应用的"当前学习焦点"与会话状态的**唯一**出处。
 *
 * 为什么放在顶层而不是 LearnView 里：
 *
 * - **知识地图要和 Tutor 联动**（当前学习节点高亮、点节点跳去学习）——
 *   这两个视图必须读到同一份"正在学什么"。
 * - 学习页的三栏（导航 / 主区 / 侧栏）共享同一份会话与状态，
 *   用 props 层层传会把 LearnView 撑成一个传参机器。
 *
 * 这里只管"状态"和"动作"，不管长什么样 —— 视图层各自去拿自己需要的部分。
 *
 * P6 起走**流式接口**：等待期间能拿到真实阶段话术与正文增量。
 * 同步接口仍在（`/api/tutor/start`、`/api/tutor/answer`），没有改动。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import {
  answerTutorStream,
  getLearnerState,
  getTutorDashboard,
  startTutorStream,
  type TutorDashboard,
  type TutorLearnerState,
  type TutorTurn,
} from '../api/tutor'
import { friendlyError } from '../features/learn/voice'
import { messageOf } from '../api/http'

export type View =
  | 'today' // 学习空间首页：今天学什么、接着学什么
  | 'learn' // Tutor 教学
  | 'library' // 我的资料
  | 'map' // 知识地图
  | 'study' // 自由学习空间：直接问任何问题，不用先选知识点
  | 'todo' // 待办清单：我要做什么（独立小功能，不接 AI、不接学习状态）
  | 'profile' // 我的：学习记录与偏好
  | 'chat' // 接口调试（仅开发者可见）

export interface LearningFocus {
  kpId: number
  title: string
  documentId: number | null
}

/** 一轮里各阶段的实际耗时（毫秒），来自运行时的工具留痕 */
export type TurnTimings = { name: string; ms: number }[]

interface LearningContextValue {
  /** 当前视图 */
  view: View
  setView: (view: View) => void
  /** 正在学 / 即将学的知识点（跨视图共享） */
  focus: LearningFocus | null
  sessionId: number | null
  turn: TutorTurn | null
  history: TutorTurn[]
  answers: string[]
  state: TutorLearnerState | null
  dashboard: TutorDashboard | null
  busy: boolean
  error: string | null
  /**
   * 后台当前在做的事（"正在看你的回答…"）。
   * 来自运行时状态机，**不是假进度** —— 有它才不会长时间白屏。
   */
  stage: string | null
  /** 正在流式生成、还没定稿的正文。边生成边显示，学习者从第一个字起就能读。 */
  streaming: string
  /** 最近一轮各阶段耗时（给开发者面板看） */
  timings: TurnTimings
  /** 开始学习（从学习页或知识地图调）—— 会切到学习视图 */
  startLearning: (focus: LearningFocus) => Promise<void>
  /** 提交一次回答 */
  submitAnswer: (text: string) => Promise<void>
  /** 结束当前会话，回到看板 */
  endSession: () => void
  /** 清掉错误提示 */
  clearError: () => void
  /** 刷新看板 */
  reloadDashboard: () => Promise<void>
}

const LearningContext = createContext<LearningContextValue | null>(null)

/** 从返回体的工具留痕里取出各阶段耗时 */
function pickTimings(turn: TutorTurn): TurnTimings {
  const tools = turn.tools as { records?: { name?: string; elapsed_ms?: number }[] } | undefined
  const records = tools?.records
  if (!Array.isArray(records)) return []
  return records
    .filter((item) => typeof item?.elapsed_ms === 'number')
    .map((item) => ({ name: String(item.name ?? '?'), ms: Number(item.elapsed_ms) }))
}

export function LearningProvider({ children }: { children: ReactNode }) {
  const [view, setView] = useState<View>('today')
  const [focus, setFocus] = useState<LearningFocus | null>(null)
  const [sessionId, setSessionId] = useState<number | null>(null)
  const [turn, setTurn] = useState<TutorTurn | null>(null)
  const [history, setHistory] = useState<TutorTurn[]>([])
  const [answers, setAnswers] = useState<string[]>([])
  const [state, setState] = useState<TutorLearnerState | null>(null)
  const [dashboard, setDashboard] = useState<TutorDashboard | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stage, setStage] = useState<string | null>(null)
  const [streaming, setStreaming] = useState('')
  const [timings, setTimings] = useState<TurnTimings>([])

  /** 用 id 防止并发时旧请求覆盖新结果 */
  const requestId = useRef(0)

  const reloadDashboard = useCallback(async () => {
    try {
      setDashboard(await getTutorDashboard())
    } catch {
      setDashboard(null)
    }
  }, [])

  useEffect(() => {
    void reloadDashboard()
  }, [reloadDashboard])

  const startLearning = useCallback(
    async (target: LearningFocus) => {
      const id = ++requestId.current
      setView('learn')
      setFocus(target)
      setBusy(true)
      setError(null)
      setTurn(null)
      setHistory([])
      setAnswers([])
      setSessionId(null)
      setStreaming('')
      setStage('正在翻你的资料…')
      try {
        const result = await startTutorStream(target.kpId, {
          onStage: setStage,
          onContent: (piece) => setStreaming((current) => current + piece),
        })
        if (id !== requestId.current) return // 已被更新的请求取代
        setTurn(result)
        setHistory([result])
        setAnswers([])
        setSessionId(result.session_id)
        setState(result.state_before)
        setTimings(pickTimings(result))
      } catch (err) {
        if (id !== requestId.current) return
        setError(friendlyError(messageOf(err)))
      } finally {
        if (id === requestId.current) {
          setBusy(false)
          setStreaming('')
          setStage(null)
        }
        void reloadDashboard()
      }
    },
    [reloadDashboard],
  )

  const submitAnswer = useCallback(
    async (text: string) => {
      const submitted = text.trim()
      if (!turn || !sessionId || !submitted) return
      const id = ++requestId.current
      setBusy(true)
      setError(null)
      setStreaming('')
      setStage('正在看你的回答…')
      try {
        const result = await answerTutorStream(sessionId, submitted, {
          onStage: setStage,
          onContent: (piece) => setStreaming((current) => current + piece),
        })
        if (id !== requestId.current) return
        setTurn(result)
        setHistory((current) => [...current, result])
        setAnswers((current) => [...current, submitted])
        setTimings(pickTimings(result))
        if (focus) setState(await getLearnerState(focus.kpId))
      } catch (err) {
        if (id !== requestId.current) return
        setError(friendlyError(messageOf(err)))
      } finally {
        if (id === requestId.current) {
          setBusy(false)
          setStreaming('')
          setStage(null)
        }
        void reloadDashboard()
      }
    },
    [turn, sessionId, focus, reloadDashboard],
  )

  const endSession = useCallback(() => {
    setSessionId(null)
    setTurn(null)
    setHistory([])
    setAnswers([])
    setStage(null)
    setStreaming('')
    void reloadDashboard()
  }, [reloadDashboard])

  const clearError = useCallback(() => setError(null), [])

  const value = useMemo<LearningContextValue>(
    () => ({
      view,
      setView,
      focus,
      sessionId,
      turn,
      history,
      answers,
      state,
      dashboard,
      busy,
      error,
      stage,
      streaming,
      timings,
      startLearning,
      submitAnswer,
      endSession,
      clearError,
      reloadDashboard,
    }),
    [
      view,
      focus,
      sessionId,
      turn,
      history,
      answers,
      state,
      dashboard,
      busy,
      error,
      stage,
      streaming,
      timings,
      startLearning,
      submitAnswer,
      endSession,
      clearError,
      reloadDashboard,
    ],
  )

  return <LearningContext.Provider value={value}>{children}</LearningContext.Provider>
}

export function useLearning(): LearningContextValue {
  const context = useContext(LearningContext)
  if (!context) {
    throw new Error('useLearning 必须在 <LearningProvider> 内使用')
  }
  return context
}
