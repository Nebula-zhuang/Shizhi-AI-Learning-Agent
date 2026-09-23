/**
 * 学习报告的接口封装。
 *
 * ⚠️ 响应里的 `mastery` / `urgency` 是**给前端做分级用的**，
 * **不许出现在界面上** —— 它们是内部度量，不是给用户看的结论。
 * 这条在项目里是硬规矩（见 `features/learn/voice.ts`）：
 * 用户可见文字里不出现置信度 / 相似度 / 数据库字段。
 * 有一条前端测试专门钉住它（`reportState.test.ts`）。
 */

import { getJson } from './http'

export interface MasteryOverview {
  /**
   * ⚠️ **全库**知识点总数 —— 不是"我的资料里有几个"。
   *
   * 它来自 `memory_service.mastery_overview`，那条查询里的
   * `select(count()).select_from(KnowledgePoint)` **没有按 learner 限定**。
   * 所以这个字段对单个用户是**错的**，`untouched` 跟着一起错。
   *
   * 后端已如实返回（不改既有函数），**前端一律不显示这两个** ✗
   * 只用 `tracked` / `new` / `learning` / `weak` / `mastered`
   * （它们来自 `where(learner_id == …)` 的那条查询 ✓）。
   */
  knowledge_point_total: number
  /** @see knowledge_point_total —— 同样不显示 */
  untouched: number
  /** 以下四个+tracked 是按 learner 限定的，可以显示 */
  tracked: number
  new: number
  learning: number
  weak: number
  mastered: number
}

export interface ReportPoint {
  knowledge_point_id: number
  title: string
  /** ⚠️ 不显示 */
  mastery: number
  /** `new` / `learning` / `weak` / `mastered` */
  status: string
  attempt_count: number
  consecutive_wrong: number
  importance: number
  last_error_type: string | null
  next_review_at: string | null
  /** ⚠️ 不显示 */
  urgency: number
  due: boolean
}

export interface TrajectoryPoint {
  at: string
  /** ⚠️ 不显示 */
  mastery: number
}

export interface Trajectory {
  knowledge_point_id: number
  title: string
  points: TrajectoryPoint[]
}

export interface LearningReport {
  generated_at: string
  window_days: number
  window_since: string
  /** `error_types` 是全量历史口径，不随窗口变化 */
  error_types_are_all_time: boolean
  overview: MasteryOverview
  weak_points: ReportPoint[]
  due_reviews: ReportPoint[]
  error_types: Record<string, number>
  sessions: { count: number; turns: number }
  trajectory: Trajectory[]
  narrative: { available: boolean; text: string }
}

export const DEFAULT_REPORT_DAYS = 30

export function getLearningReport(days: number = DEFAULT_REPORT_DAYS): Promise<LearningReport> {
  return getJson<LearningReport>(`/api/reports/learning?days=${days}`)
}
