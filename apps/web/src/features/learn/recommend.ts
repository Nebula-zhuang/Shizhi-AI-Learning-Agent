/**
 * 「下一步学什么」的推荐逻辑。
 *
 * 单独抽出来是因为**中栏和右栏都要用它**：中栏给主按钮，右栏给建议卡。
 * 两处各写一遍迟早会不一致（一边推荐 A、一边推荐 B），
 * 所以只有这一个函数说了算。
 *
 * 优先级就是"什么最该现在做"：
 *   1. 到期的复习 —— 遗忘曲线在提醒，且成本最低（刚学过）
 *   2. 摔过跟头的 —— 没掌握的地方最该补
 *   3. 都没有 —— 交给学习者自己从左边挑
 */

import type { TutorDashboard } from '../../api/tutor'

export interface Recommendation {
  kpId: number
  title: string
  /** 为什么推荐它（一句话，给人看） */
  reason: string
}

export function recommendNext(dashboard: TutorDashboard | null): Recommendation | null {
  if (!dashboard) return null

  const due = dashboard.due_reviews[0]
  if (due) {
    return {
      kpId: due.knowledge_point_id,
      title: due.title,
      reason: '这个到了该复习的时候',
    }
  }

  const weak = dashboard.weak_points[0]
  if (weak) {
    return {
      kpId: weak.knowledge_point_id,
      title: weak.title,
      reason: '这里你上次卡住了',
    }
  }

  return null
}
