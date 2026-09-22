/**
 * 对话搜索的**纯逻辑**。
 *
 * ## 为什么单独一个文件
 *
 * 前端单测跑的是 `node --experimental-strip-types --test "src/**\/*.test.ts"`
 * —— **只收 `.ts`，不收 `.tsx`**。所以"按什么匹配、怎么算没匹配上"
 * 这部分抽到本模块，才能被真正测到；组件只负责把结果画出来。
 *
 * ## 为什么是纯前端过滤
 *
 * 自由学习页的对话列表**本来就已经全量加载在内存里**（`listConversations`
 * 一次取回），所以过滤不需要后端 —— 加一个接口只会多一次往返、
 * 还要处理"搜索结果的归属"，而收益是零。
 *
 * ⚠️ 代价：**只搜标题，不搜对话正文**。正文在 `conversation_messages` 里，
 * 那才是真需要后端的地方。这一轮刻意不做 —— 界面上也不要暗示能搜正文。
 */

import type { ConversationSummary } from '../../api/study'

/**
 * 按关键词过滤对话。**只匹配标题**（见文件开头）。
 *
 * 空关键词 → 原样返回**同一个数组引用**，让 `useMemo` 的消费方
 * 不必因为"内容一样但是新数组"而重渲染。
 */
export function filterConversations(
  conversations: readonly ConversationSummary[],
  query: string,
): readonly ConversationSummary[] {
  const keyword = query.trim().toLowerCase()
  if (!keyword) return conversations
  return conversations.filter((item) => item.title.toLowerCase().includes(keyword))
}

/**
 * 列表下方那句话。**三种情况三种说法，而不是一套文案硬套**：
 *
 * | 情况 | 说什么 |
 * |---|---|
 * | 一条对话都没有 | 「还没有对话。问第一个问题就会出现在这里。」 |
 * | 有对话、但没在搜 | **什么都不说**（列表自己会说话） |
 * | 搜了、没匹配上 | 「没有标题包含「X」的对话。」 |
 *
 * ⚠️ 第二、三种必须分开：把"你还没有对话"说给一个搜了关键词的人听，
 * 等于答非所问 —— 他不知道是自己的词没匹配上，会以为对话被删了。
 */
export function emptyListHint(query: string, total: number): string {
  if (total === 0) return '还没有对话。问第一个问题就会出现在这里。'
  if (!query.trim()) return ''
  return `没有标题包含「${query.trim()}」的对话。`
}

/**
 * 「搜到几条」的说明文字。**只在真的在搜的时候给** ——
 * 平时显示"N 个对话"是没话找话，反而让列表显得吵。
 */
export function matchSummary(query: string, matched: number, total: number): string {
  if (!query.trim()) return ''
  return `找到 ${matched} / ${total} 个`
}
