/**
 * 学习意图（后端 5B 的输出，前端 5C 消费）—— 类型与解析。
 *
 * ## 为什么单独一个文件，而不是放在 `api/study.ts` 里
 *
 * `api/study.ts` 会 `import { getJson } from './http'`（**运行时**依赖，且不带扩展名）。
 * 而前端单测跑的是 `node --experimental-strip-types`，它**按原样解析模块路径** ——
 * 于是任何"从 `api/study` 导入一个**值**"的测试都会
 * `ERR_MODULE_NOT_FOUND` ✗。
 *
 * （`savedKnowledge.test.ts` / `conversationSearch.test.ts` 之所以没事，
 * 是因为它们从 `api/study` 只导入 **type** —— 类型会被剥离，不留运行时引用。）
 *
 * 所以：**解析逻辑放在这个零依赖的小文件里**，`api/study.ts` 与
 * `features/study/learnProposal.ts` 都从这里引用。
 * `api/study.ts` 仍然 re-export 它，外部用法不变。
 */

/**
 * 学习意图。
 *
 * 后端**只在判断出"他想学一个主题"时才带这个字段** —— 普通提问的 `done` 里
 * 根本没有它。所以消费方必须把它当**可选**：**"键在不在"就是最强的信号**，
 * 不要指望用一个恒为 null 的值来表示"没有意图"。
 */
export interface LearnSuggestion {
  /** 想学的主题名（后端已截到 ≤30 字） */
  topic: string
  /** `topic` 从零学一个新主题／`material` 借自己的资料学一遍 */
  kind: 'topic' | 'material'
}

/**
 * 把 `done` 里的 `learn_suggestion` 收成合法结构。**拿不准就返回 null。**
 *
 * 与后端 `_parse_learn_suggestion` 同一套规矩：
 * 字段缺失、类型不对、主题为空 → 一律视为"没有意图"。
 *
 * ⚠️ **不能因为"看起来像是在问东西"就凑一张提议卡出来** ——
 * 用户只是问了个概念，却被反问"要不要开始学习"，比不提议更烦人。
 *
 * `kind` 带空格时先 trim 再判断（与后端一致）：合法的值应当被救回来，
 * 而不是因为一个多余空格就降级。
 */
export function parseLearnSuggestion(raw: unknown): LearnSuggestion | null {
  if (!raw || typeof raw !== 'object') return null
  const block = raw as Record<string, unknown>

  const topic = String(block.topic ?? '').trim()
  if (!topic) return null

  const kind = String(block.kind ?? '').trim()
  return { topic, kind: kind === 'material' ? 'material' : 'topic' }
}
