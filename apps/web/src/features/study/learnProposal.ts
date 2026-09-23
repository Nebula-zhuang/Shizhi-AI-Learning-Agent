/**
 * 学习提议卡的**纯逻辑**。
 *
 * ## 为什么单独一个文件
 *
 * 前端单测只收 `.ts`（`node --experimental-strip-types --test "src/**\/*.test.ts"`），
 * `.tsx` 组件测不到。所以"该不该显示卡、显示什么话"这部分抽出来，才测得到。
 *
 * ## 一条最重要的规矩
 *
 * **没有 `learnSuggestion` 就什么都不显示。**
 *
 * 后端只在判断出"他想学一个主题"时才带那个字段。前端**不需要、也不应该**
 * 再做一次判断 —— 任何"看起来像在问学习"的启发式都会让用户问一个概念时
 * 被反问"要不要开始学习"，那比不提议更烦人。
 *
 * 所以这里的入口只有一个：拿到 suggestion 才出卡，拿不到就 null。
 */

import type { LearnSuggestion } from '../../api/learnSuggestion'

export interface LearnProposalCard {
  /** 卡片主句 */
  title: string
  /** 一句说明，讲清两个按钮分别会发生什么 */
  description: string
  keepLabel: string
  startLabel: string
}

/** 两个按钮的文案。**固定不变** —— 用户在别的页面见过同样的说法。 */
export const KEEP_LABEL = '继续自由学习'
export const START_LABEL = '开始学习'

/**
 * 生成卡片文案。**`undefined` → `null`（不显示任何东西）**。
 *
 * `kind` 只影响那句说明：
 * - `topic` —— 他刚表达了想学一个新主题，可以去「辅导」从知识点开始
 * - `material` —— 他想借自己的资料学，说明"去辅导挑一个相关知识点"更贴切
 */
export function learnProposalCard(
  suggestion: LearnSuggestion | undefined,
): LearnProposalCard | null {
  if (!suggestion) return null
  const topic = suggestion.topic.trim()
  if (!topic) return null

  const description =
    suggestion.kind === 'material'
      ? `这一轮是照着你的资料讲的。想按知识点一步步练，可以去「辅导」挑一个相关的开始。`
      : `想系统学「${topic}」，可以去「辅导」按知识点一步步来；也可以留在这儿接着问。`

  return {
    title: `想学「${topic}」？`,
    description,
    keepLabel: KEEP_LABEL,
    startLabel: START_LABEL,
  }
}

/**
 * 点「继续自由学习」时，替用户填进输入框的那句话。
 *
 * ⚠️ **只填不发。** 替用户把消息发出去等于替他做了决定 ——
 * 他可能只是想先看看，或者想换个说法问。填好、聚焦，
 * 按不按回车是他自己的事。
 */
export function continuePrompt(suggestion: LearnSuggestion): string {
  const topic = suggestion.topic.trim()
  return suggestion.kind === 'material'
    ? `带我按资料把「${topic}」过一遍`
    : `带我从头学一下「${topic}」`
}
