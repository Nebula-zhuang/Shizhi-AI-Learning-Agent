/**
 * 「保存知识」的**纯逻辑**。
 *
 * ## 为什么要把这些判断单独放在这里
 *
 * 前端的单测跑的是 `node --experimental-strip-types --test "src/**\/*.test.ts"`
 * —— **只收 `.ts`，不收 `.tsx`**（现有三个测试文件全是纯逻辑）。
 * 组件级测试没有基建，所以凡是"判断对不对"的部分都抽到本模块，
 * 让它们可以被真正测到；`FreeStudyView` 只剩"把结果画出来"。
 *
 * ## 一条贯穿全文的规矩
 *
 * **没有服务端 `message_id` 就不给保存入口。**
 * 保存必须凭 id 完成（正文由服务端从消息里取），
 * 让界面出现一个注定 404 的按钮，比不给按钮更糟。
 */

import type { SavedKnowledgeItem } from '../../api/study'

// --------------------------------------------------------------------------- //
// 可保存性
// --------------------------------------------------------------------------- //
export type SaveBlockReason =
  | 'ok'
  /** 不是助手的回答 —— 保存提问没有意义 */
  | 'not-assistant'
  /** 还没落库（或落库失败），拿不到服务端 id */
  | 'no-message-id'
  /** 正在流式生成，落库都还没发生 */
  | 'streaming'
  /** 正文为空 —— 存下来也是一条空记录 */
  | 'empty'
  /** 已经存过了 */
  | 'already-saved'

export interface SaveEligibility {
  canSave: boolean
  reason: SaveBlockReason
  /** 不能保存时给用户看的一句话；能保存时为空 */
  hint: string
}

/** 判断这一轮能不能保存。不含任何副作用，可放心单测。 */
export function saveEligibility(
  turn: { role: 'user' | 'assistant'; content: string; streaming: boolean; messageId?: number },
  savedMessageIds: ReadonlySet<number>,
): SaveEligibility {
  if (turn.role !== 'assistant') {
    return { canSave: false, reason: 'not-assistant', hint: '只能保存助手的回答。' }
  }
  if (turn.streaming) {
    return { canSave: false, reason: 'streaming', hint: '等这条回答说完再保存。' }
  }
  if (turn.messageId === undefined || !Number.isInteger(turn.messageId) || turn.messageId <= 0) {
    // 落库失败 / 历史里没带 id —— 不显示可点击的保存入口
    return { canSave: false, reason: 'no-message-id', hint: '这条还没存好，暂时不能保存。' }
  }
  if (!turn.content.trim()) {
    return { canSave: false, reason: 'empty', hint: '这条回答是空的，没有可保存的内容。' }
  }
  if (savedMessageIds.has(turn.messageId)) {
    return { canSave: false, reason: 'already-saved', hint: '已经保存过了。' }
  }
  return { canSave: true, reason: 'ok', hint: '' }
}

/** 按钮上显示什么。已保存是**状态**，不是可点的动作。 */
export function saveButtonLabel(eligibility: SaveEligibility, saving: boolean): string {
  if (saving) return '保存中…'
  if (eligibility.reason === 'already-saved') return '已保存'
  return '保存知识'
}

/** 已保存的知识用了哪些 message（用来判断"这条是否已保存"）。 */
export function savedMessageIdsOf(items: readonly SavedKnowledgeItem[]): Set<number> {
  const ids = new Set<number>()
  for (const item of items) {
    if (typeof item.source_message_id === 'number') ids.add(item.source_message_id)
  }
  return ids
}

// --------------------------------------------------------------------------- //
// 失败文案
// --------------------------------------------------------------------------- //
/**
 * 把保存失败翻成人话。
 *
 * 后端对"消息不存在 / 不是你的 / 不是助手消息"**统一返回 404**（防枚举），
 * 所以这里也**不猜具体原因** —— 只给一条可执行的建议。
 */
export function saveFailureMessage(status: number | undefined, detail?: string): string {
  if (status === 404) {
    // 最可能是"这条消息已经不在库里了"（比如它所在的那个对话被删掉）
    return '没找到这条回答 —— 它所在的对话可能已经被删了。'
  }
  if (status === 401) return '登录状态过期了，请重新登录后再试。'
  if (detail && detail.trim()) return detail.trim()
  return '保存失败，稍后再试一次。'
}

/** 从各种异常里抽出 HTTP 状态码（没有就返回 undefined）。 */
export function statusOf(error: unknown): number | undefined {
  if (typeof error === 'object' && error !== null && 'status' in error) {
    const value = (error as { status?: unknown }).status
    if (typeof value === 'number') return value
  }
  return undefined
}

// --------------------------------------------------------------------------- //
// 列表分页
// --------------------------------------------------------------------------- //
/**
 * 把新一页拼到已有列表后面，**按 id 去重**。
 *
 * 去重的必要性：用户可能连续刷新两次，或翻页时后端刚好插入了新条目，
 * 于是同一条出现在两页里 —— 不去重就会看到重复卡片，
 * 而且 React 的 key 也会撞。
 */
export function mergeSavedItems(
  existing: readonly SavedKnowledgeItem[],
  page: readonly SavedKnowledgeItem[],
): SavedKnowledgeItem[] {
  const seen = new Set(existing.map((item) => item.id))
  const merged = [...existing]
  for (const item of page) {
    if (seen.has(item.id)) continue
    seen.add(item.id)
    merged.push(item)
  }
  return merged
}

/** 还有没有下一页（按 total 判断，而不是"这一页是否满"）。 */
export function hasMoreSaved(loaded: number, total: number): boolean {
  return loaded < total
}

/** 保存时间显示成 `2026-09-22`。取不到就返回空串，不编一个假日期。 */
export function formatSavedAt(createdAt: string | null | undefined): string {
  if (!createdAt) return ''
  const date = new Date(createdAt)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** 列表里那一行的标题：优先问题，其次回答开头，都没有就给个占位。 */
export function savedItemTitle(item: SavedKnowledgeItem, maxChars = 40): string {
  const source = item.question.trim() || item.answer.trim().replace(/\s+/g, ' ')
  if (!source) return '（无标题）'
  return source.length > maxChars ? `${source.slice(0, maxChars)}…` : source
}
