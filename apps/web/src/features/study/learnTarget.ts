/**
 * 「开始学习」的**纯逻辑**：把后端的匹配结果翻译成"去辅导"或"降级提示"。
 *
 * ## 一条绝不能破的规矩
 *
 * **拿不到可信的 `kpId` 就不许去开教学。**
 *
 * `startLearning(focus)` 会**立刻发起一轮真实教学**（后端 `require_knowledge_point`
 * 校验）。所以只要 id 是空的、或 `matched` 与 `kp_id` 互相矛盾，
 * 就必须走降级 —— 否则用户会在一门完全没想学的课里被问第一个问题。
 *
 * 所以这里**不看后端说 matched 就信**：两个条件都满足才算匹配成功。
 */

import type { LearnTargetResponse } from '../../api/study'
import type { LearningFocus } from '../../app/LearningProvider'

/** 匹配成功：可以直接开教学 */
export interface LearnGo {
  kind: 'go'
  focus: LearningFocus
}

/** 没匹配上：留在自由学习，或去辅导自己挑 */
export interface LearnStay {
  kind: 'stay'
  /** 一句说明，讲清发生了什么、接下来能做什么 */
  hint: string
}

export type LearnOutcome = LearnGo | LearnStay

/** 降级时两个选项的文案（与提议卡保持一致的说法）。 */
export const STAY_LABEL = '继续自由学习'
export const PICKER_LABEL = '去辅导挑一个'

/**
 * 决定"点了开始学习之后会发生什么"。
 *
 * 匹配成功的判据是**两个**：后端说 `matched`，**且**给出一个正整数 `kp_id`。
 * 少任何一个都走降级 —— 只信 `matched` 会踩到"字段缺失但标记为真"的脏数据。
 */
export function learnOutcome(result: LearnTargetResponse): LearnOutcome {
  const kpId = Number(result.kp_id)
  const usable =
    result.matched === true && Number.isInteger(kpId) && kpId > 0 && Boolean(result.title?.trim())

  if (usable) {
    return {
      kind: 'go',
      focus: {
        kpId,
        title: result.title.trim(),
        documentId: result.document_id ?? null,
      },
    }
  }

  return { kind: 'stay', hint: stayHint(result.reason) }
}

/**
 * 降级提示的文案。**按后端给的机器码分流** ——
 * 后端只给码，文案归前端（后端字段不许直接拼进界面）。
 *
 * ⚠️ 出现"歧义"时**不替用户猜**，而是明确告诉他去自己挑 ——
 * 替他猜一个的代价是他被带进一个错的知识点。
 */
export function stayHint(reason: string): string {
  if (reason === 'ambiguous') {
    return '你的资料里有好几个都叫得上的知识点，这个我不替你挑 —— 去「辅导」看一眼再选。'
  }
  return '你的资料里好像还没讲到这个，先在「辅导」里挑一个知识点开始吧。'
}
