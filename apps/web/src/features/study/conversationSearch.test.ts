/**
 * 对话搜索纯逻辑测试。
 *
 * ## 这组测试守两件事
 *
 * 1. **只在真的搜的时候才给反馈。** 平时显示"找到 5 / 5 个"是没话找话；
 *    没有搜索词时必须返回空串，界面才会安静。
 * 2. **"没搜到"与"本来就没有"要说不同的话。** 把"你还没有对话"
 *    说给一个搜了关键词的人听，等于答非所问 —— 他不知道是自己的词没匹配上。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  emptyListHint,
  filterConversations,
  matchSummary,
} from './conversationSearch.ts'
import type { ConversationSummary } from '../../api/study'

function conv(id: number, title: string): ConversationSummary {
  return {
    id,
    title,
    message_count: 2,
    created_at: '2026-09-22T10:00:00',
    last_message_at: '2026-09-22T10:05:00',
  }
}

const ROWS = [
  conv(1, '什么是 JVM？'),
  conv(2, '虚拟线程和平台线程'),
  conv(3, 'Java 内存模型'),
  conv(4, ''),
]

// --------------------------------------------------------------------------- //
// 一、过滤
// --------------------------------------------------------------------------- //
test('空搜索词返回原数组本身（不只是内容相同）', () => {
  // 返回新数组会让 useMemo 的消费方以为变了、白重渲染一次
  assert.equal(filterConversations(ROWS, ''), ROWS)
  assert.equal(filterConversations(ROWS, '   '), ROWS)
})

test('按标题子串匹配', () => {
  const got = filterConversations(ROWS, '线程')
  assert.deepEqual(got.map((c) => c.id), [2])
})

test('匹配多个', () => {
  const got = filterConversations(ROWS, 'Java')
  assert.deepEqual(got.map((c) => c.id), [3], '只有第 3 条标题里有 Java')
})

test('大小写不敏感', () => {
  assert.deepEqual(filterConversations(ROWS, 'jvm').map((c) => c.id), [1])
  assert.deepEqual(filterConversations(ROWS, 'JVM').map((c) => c.id), [1])
  assert.deepEqual(filterConversations(ROWS, 'javascript').map((c) => c.id), [])
})

test('中文子串也匹配', () => {
  assert.deepEqual(filterConversations(ROWS, '什么').map((c) => c.id), [1])
})

test('搜索词前后的空格被忽略', () => {
  assert.deepEqual(filterConversations(ROWS, '  线程  ').map((c) => c.id), [2])
})

test('标题为空的对话不会被非空查询命中', () => {
  // 第 4 条标题是空串。搜任何**非空**词都不该把它捞出来
  // （空白查询不算"非空"—— 见下一条）
  for (const q of ['a', 'JVM', '什么', 'Java']) {
    const got = filterConversations(ROWS, q)
    assert.ok(!got.some((c) => c.id === 4), `查询 ${JSON.stringify(q)} 不该命中空标题`)
  }
})

test('只有空白字符的查询等同于没查询，会返回全部（含空标题那条）', () => {
  const got = filterConversations(ROWS, '   ')
  assert.equal(got, ROWS, '空白查询应当原样返回，不做任何过滤')
  assert.ok(got.some((c) => c.id === 4))
})

test('没匹配上时返回空数组', () => {
  assert.deepEqual(filterConversations(ROWS, '不存在的词'), [])
})

test('空列表怎么搜都是空', () => {
  assert.deepEqual(filterConversations([], 'JVM'), [])
  assert.equal(filterConversations([], '').length, 0)
})

test('不改动入参', () => {
  const before = ROWS.map((c) => c.id)
  filterConversations(ROWS, 'JVM')
  assert.deepEqual(ROWS.map((c) => c.id), before)
})

// --------------------------------------------------------------------------- //
// 二、"没搜到"与"本来就没有"必须分开说
// --------------------------------------------------------------------------- //
test('一条对话都没有时，无论有没有搜索词都说"还没有对话"', () => {
  const expected = '还没有对话。问第一个问题就会出现在这里。'
  assert.equal(emptyListHint('', 0), expected)
  assert.equal(emptyListHint('   ', 0), expected)
  assert.equal(emptyListHint('JVM', 0), expected)
})

test('有对话、又没在搜时不说话（列表自己会说话）', () => {
  assert.equal(emptyListHint('', 4), '')
  assert.equal(emptyListHint('   ', 4), '')
})

test('有对话但没匹配上时，要把搜索词还回去', () => {
  const hint = emptyListHint('Kotlin', 4)
  assert.ok(hint.includes('Kotlin'), '不指名搜索词，用户不知道是哪次搜索的结果')
  assert.ok(!hint.includes('还没有对话'), '不能说成"还没有对话"，那是在答非所问')
})

test('提示里的搜索词是 trim 过的', () => {
  assert.ok(emptyListHint('  Kotlin  ', 4).includes('「Kotlin」'))
})

// --------------------------------------------------------------------------- //
// 三、结果计数只在搜索时出现
// --------------------------------------------------------------------------- //
test('没有搜索词时不显示计数', () => {
  assert.equal(matchSummary('', 5, 5), '')
  assert.equal(matchSummary('  ', 5, 5), '')
})

test('搜索中显示 命中/总数', () => {
  assert.equal(matchSummary('JVM', 1, 4), '找到 1 / 4 个')
  assert.equal(matchSummary('JVM', 0, 4), '找到 0 / 4 个')
})
