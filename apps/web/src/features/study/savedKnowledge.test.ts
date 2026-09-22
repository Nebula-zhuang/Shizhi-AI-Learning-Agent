/**
 * 「保存知识」纯逻辑测试。
 *
 * ## 这组测试要钉死的一件事
 *
 * **没有服务端 `message_id` 就不能保存。**
 *
 * 保存的正文由服务端从消息里取，前端只能给 id —— 所以一个没有 id 的
 * 助手消息，点了保存**必然 404**。界面如果在这种消息上显示可点的
 * "保存知识"，就是在给用户挖坑：他能点的每一个按钮都注定失败。
 *
 * `saveEligibility` 的 `no-message-id` 分支就是为它而写的，
 * 下面第一组用例把它连同其他几种情况一起钉住。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  formatSavedAt,
  hasMoreSaved,
  mergeSavedItems,
  saveButtonLabel,
  saveEligibility,
  saveFailureMessage,
  savedItemTitle,
  savedMessageIdsOf,
  statusOf,
  type SaveBlockReason,
} from './savedKnowledge.ts'
import type { SavedKnowledgeItem } from '../../api/study'

const NO_SAVED: ReadonlySet<number> = new Set<number>()

function assistant(overrides: Partial<Parameters<typeof saveEligibility>[0]> = {}) {
  return {
    role: 'assistant' as const,
    content: 'JVM 是 Java 虚拟机。',
    streaming: false,
    messageId: 123,
    ...overrides,
  }
}

function item(overrides: Partial<SavedKnowledgeItem> = {}): SavedKnowledgeItem {
  return {
    id: 1,
    question: '什么是 JVM？',
    answer: 'Java 虚拟机。',
    source_message_id: 123,
    source_urls: null,
    kp_ids: null,
    tags: null,
    embedding_id: 'saved:1',
    created_at: '2026-09-22T14:00:00',
    ...overrides,
  }
}

// --------------------------------------------------------------------------- //
// 一、可保存性：正常情况
// --------------------------------------------------------------------------- //
test('落库过的助手回答可以保存', () => {
  const result = saveEligibility(assistant(), NO_SAVED)
  assert.equal(result.canSave, true)
  assert.equal(result.reason, 'ok')
  assert.equal(result.hint, '', '能保存时不该有提示语')
})

// --------------------------------------------------------------------------- //
// 二、不该出现保存入口的情况（逐条钉住）
// --------------------------------------------------------------------------- //
test('没有 message_id 时不可保存 —— 这是最重要的一条', () => {
  // 落库失败、或历史数据没带 id 时会走到这里
  const result = saveEligibility(assistant({ messageId: undefined }), NO_SAVED)
  assert.equal(result.canSave, false)
  assert.equal(result.reason, 'no-message-id')
  assert.ok(result.hint.length > 0, '必须给一句解释，不能只是灰掉')
})

test('message_id 是非法值时也不可保存', () => {
  for (const bad of [0, -1, 1.5, Number.NaN]) {
    const result = saveEligibility(assistant({ messageId: bad }), NO_SAVED)
    assert.equal(result.canSave, false, `messageId=${bad} 时不该可保存`)
    assert.equal(result.reason, 'no-message-id')
  }
})

test('正在流式输出的那条不可保存', () => {
  const result = saveEligibility(assistant({ streaming: true }), NO_SAVED)
  assert.equal(result.canSave, false)
  assert.equal(result.reason, 'streaming')
})

test('空回答不可保存', () => {
  for (const content of ['', '   ', '\n\n']) {
    const result = saveEligibility(assistant({ content }), NO_SAVED)
    assert.equal(result.canSave, false, `content=${JSON.stringify(content)} 时不该可保存`)
    assert.equal(result.reason, 'empty')
  }
})

test('用户自己的提问不可保存', () => {
  const result = saveEligibility(
    { role: 'user', content: '什么是 JVM？', streaming: false, messageId: 9 },
    NO_SAVED,
  )
  assert.equal(result.canSave, false)
  assert.equal(result.reason, 'not-assistant')
})

test('已经保存过的不再提供保存动作', () => {
  const saved = new Set([123])
  const result = saveEligibility(assistant(), saved)
  assert.equal(result.canSave, false)
  assert.equal(result.reason, 'already-saved')
})

test('已保存判断依据是 messageId，而不是内容', () => {
  // 同样的内容、不同的 messageId → 仍然可以保存（是另一轮回答）
  const saved = new Set([999])
  assert.equal(saveEligibility(assistant({ messageId: 123 }), saved).canSave, true)
  // 不同的内容、相同的 messageId → 已保存
  const other = assistant({ content: '换了一段正文。' })
  assert.equal(saveEligibility(other, new Set([123])).reason, 'already-saved')
})

test('每一条阻塞原因都有自己的文案，不会漏', () => {
  const reasons: SaveBlockReason[] = [
    'not-assistant',
    'no-message-id',
    'streaming',
    'empty',
    'already-saved',
  ]
  const cases = [
    saveEligibility({ role: 'user', content: 'x', streaming: false, messageId: 1 }, NO_SAVED),
    saveEligibility(assistant({ messageId: undefined }), NO_SAVED),
    saveEligibility(assistant({ streaming: true }), NO_SAVED),
    saveEligibility(assistant({ content: '' }), NO_SAVED),
    saveEligibility(assistant(), new Set([123])),
  ]
  assert.deepEqual(
    cases.map((c) => c.reason),
    reasons,
  )
  for (const result of cases) {
    assert.ok(result.hint.trim().length > 0, `${result.reason} 缺文案`)
  }
})

// --------------------------------------------------------------------------- //
// 三、按钮文案
// --------------------------------------------------------------------------- //
test('按钮文案随状态变化', () => {
  const ok = saveEligibility(assistant(), NO_SAVED)
  assert.equal(saveButtonLabel(ok, false), '保存知识')
  assert.equal(saveButtonLabel(ok, true), '保存中…', '保存中要挡住重复点击')

  const saved = saveEligibility(assistant(), new Set([123]))
  assert.equal(saveButtonLabel(saved, false), '已保存')
  assert.equal(saveButtonLabel(saved, true), '保存中…', '保存中优先于已保存')
})

// --------------------------------------------------------------------------- //
// 四、已保存集合
// --------------------------------------------------------------------------- //
test('从列表里取出已保存的 message 集合', () => {
  const ids = savedMessageIdsOf([
    item({ id: 1, source_message_id: 11 }),
    item({ id: 2, source_message_id: 22 }),
    item({ id: 3, source_message_id: null }), // 没有回溯信息，跳过
  ])
  assert.deepEqual([...ids].sort((a, b) => a - b), [11, 22])
})

test('空列表得到空集合', () => {
  assert.equal(savedMessageIdsOf([]).size, 0)
})

// --------------------------------------------------------------------------- //
// 五、失败文案：不能猜原因
// --------------------------------------------------------------------------- //
test('404 给出"对话可能被删了"而不是猜具体原因', () => {
  // 后端对"不存在 / 不是你的 / 不是助手消息"统一返回 404（防枚举），
  // 所以前端也不该假装分得清
  const text = saveFailureMessage(404)
  assert.ok(text.includes('对话'), '404 最可能是消息随对话被删')
  assert.ok(!text.includes('不属于你'), '不能替后端断定"不是你的"')
})

test('401 提示重新登录', () => {
  assert.ok(saveFailureMessage(401).includes('登录'))
})

test('有 detail 时优先用它，没有则给兜底', () => {
  assert.equal(saveFailureMessage(400, '这条回答不存在。'), '这条回答不存在。')
  assert.equal(saveFailureMessage(400, '   '), '保存失败，稍后再试一次。')
  assert.equal(saveFailureMessage(undefined), '保存失败，稍后再试一次。')
})

test('从异常里抽状态码', () => {
  assert.equal(statusOf({ status: 404 }), 404)
  assert.equal(statusOf(new Error('boom')), undefined, '普通 Error 没有 status')
  assert.equal(statusOf(null), undefined)
  assert.equal(statusOf('404'), undefined)
})

// --------------------------------------------------------------------------- //
// 六、分页拼接
// --------------------------------------------------------------------------- //
test('分页拼接按 id 去重并保持顺序', () => {
  const first = [item({ id: 3 }), item({ id: 2 })]
  const second = [item({ id: 2 }), item({ id: 1 })] // 2 重复
  const merged = mergeSavedItems(first, second)
  assert.deepEqual(
    merged.map((i) => i.id),
    [3, 2, 1],
    '重复的 2 只保留第一次出现的位置',
  )
})

test('拼空页不改变原列表', () => {
  const first = [item({ id: 1 })]
  assert.deepEqual(mergeSavedItems(first, []).map((i) => i.id), [1])
})

test('是否还有下一页看 total，不看这一页满不满', () => {
  assert.equal(hasMoreSaved(0, 5), true)
  assert.equal(hasMoreSaved(5, 5), false)
  assert.equal(hasMoreSaved(4, 5), true)
  // 空结果是 0/0，不该显示"加载更多"
  assert.equal(hasMoreSaved(0, 0), false)
})

// --------------------------------------------------------------------------- //
// 七、展示小工具
// --------------------------------------------------------------------------- //
test('时间格式化成日期', () => {
  assert.equal(formatSavedAt('2026-09-22T14:00:00'), '2026-09-22')
})

test('取不到时间就返回空串，不编一个假日期', () => {
  for (const bad of [null, undefined, '', '不是时间']) {
    assert.equal(formatSavedAt(bad as string | null | undefined), '')
  }
})

test('列表标题优先用问题', () => {
  assert.equal(savedItemTitle(item({ question: '什么是 JVM？' })), '什么是 JVM？')
})

test('没有问题时退回回答的开头', () => {
  const title = savedItemTitle(
    item({ question: '', answer: 'JVM 是 Java 虚拟机，\n负责执行字节码。' }),
  )
  assert.equal(title, 'JVM 是 Java 虚拟机， 负责执行字节码。', '换行要压成空格')
})

test('标题过长要截断', () => {
  const long = '很'.repeat(100)
  const title = savedItemTitle(item({ question: long }), 40)
  assert.equal(title.length, 41, '40 个字符 + 省略号')
  assert.ok(title.endsWith('…'))
})

test('问题与回答都空时给占位而不是空标题', () => {
  assert.equal(savedItemTitle(item({ question: '', answer: '' })), '（无标题）')
})
