/**
 * 学习提议卡纯逻辑测试。
 *
 * ## 这组测试守的核心只有一条
 *
 * **没有 `learnSuggestion` 就什么都不显示。**
 *
 * 后端只在判断出"他想学一个主题"时才带那个字段。前端若自己再猜一次
 * （关键词、问句形式……），用户问「什么是 JVM」时就可能被反问
 * "要不要开始学习" —— 比不提议更烦人。所以 `undefined` / 空主题
 * 必须**一律 null**，下面用例把它钉死。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  KEEP_LABEL,
  START_LABEL,
  continuePrompt,
  learnProposalCard,
} from './learnProposal.ts'
// ⚠️ 必须从 `api/learnSuggestion`（零依赖）导入，**不能**从 `api/study` ——
// 后者会 `import './http'`（运行时、且不带扩展名），
// 而 `--experimental-strip-types` 会真的去解析路径 → ERR_MODULE_NOT_FOUND。
import { parseLearnSuggestion } from '../../api/learnSuggestion.ts'

// --------------------------------------------------------------------------- //
// 一、不显示的情况
// --------------------------------------------------------------------------- //
test('没有 suggestion 时完全不显示', () => {
  assert.equal(learnProposalCard(undefined), null)
})

test('主题为空/只有空格时不显示', () => {
  assert.equal(learnProposalCard({ topic: '', kind: 'topic' }), null)
  assert.equal(learnProposalCard({ topic: '   ', kind: 'topic' }), null)
})

// --------------------------------------------------------------------------- //
// 二、显示的内容
// --------------------------------------------------------------------------- //
test('想学主题：标题带上主题名，两个按钮文案固定', () => {
  const card = learnProposalCard({ topic: 'Java 线程', kind: 'topic' })
  assert.ok(card)
  assert.equal(card.title, '想学「Java 线程」？')
  assert.equal(card.keepLabel, KEEP_LABEL)
  assert.equal(card.startLabel, START_LABEL)
  assert.equal(card.keepLabel, '继续自由学习')
  assert.equal(card.startLabel, '开始学习')
})

test('主题名两边的空格被清掉', () => {
  const card = learnProposalCard({ topic: '  Java 线程  ', kind: 'topic' })
  assert.ok(card)
  assert.equal(card.title, '想学「Java 线程」？')
})

test('两种 kind 的说明不同，但标题一致', () => {
  const topic = learnProposalCard({ topic: 'Java 线程', kind: 'topic' })
  const material = learnProposalCard({ topic: '第三章', kind: 'material' })
  assert.ok(topic && material)
  assert.equal(topic.title, '想学「Java 线程」？')
  assert.equal(material.title, '想学「第三章」？')
  assert.notEqual(topic.description, material.description, '两种意图的说明应当不同')
})

test('说明里要说清"还可以留在这里"', () => {
  const card = learnProposalCard({ topic: 'Java 线程', kind: 'topic' })
  assert.ok(card)
  // 两个按钮是并列选项，不能让人以为非走不可
  assert.ok(card.description.includes('接着问') || card.description.includes('留在这儿'))
})

// --------------------------------------------------------------------------- //
// 三、继续自由学习：只填不发
// --------------------------------------------------------------------------- //
test('继续自由学习的追问带上主题名', () => {
  assert.equal(continuePrompt({ topic: 'Java 线程', kind: 'topic' }), '带我从头学一下「Java 线程」')
  assert.equal(continuePrompt({ topic: '第三章', kind: 'material' }), '带我按资料把「第三章」过一遍')
})

test('追问里的主题名也清空格', () => {
  assert.ok(continuePrompt({ topic: '  Java 线程 ', kind: 'topic' }).includes('「Java 线程」'))
})

// --------------------------------------------------------------------------- //
// 四、SSE 解析：与后端同一套规矩
// --------------------------------------------------------------------------- //
test('解析合法的 learn_suggestion', () => {
  assert.deepEqual(parseLearnSuggestion({ topic: 'Java 线程', kind: 'topic' }), {
    topic: 'Java 线程',
    kind: 'topic',
  })
  assert.deepEqual(parseLearnSuggestion({ topic: '第三章', kind: 'material' }), {
    topic: '第三章',
    kind: 'material',
  })
})

test('普通提问（字段缺失）解析成 null —— 这是最常见的情况', () => {
  for (const raw of [undefined, null, {}, 'Java 线程', [], 0]) {
    assert.equal(parseLearnSuggestion(raw), null, `${JSON.stringify(raw)} 应当是 null`)
  }
})

test('残缺的字段整条丢掉，不要半条', () => {
  assert.equal(parseLearnSuggestion({ topic: '' }), null)
  assert.equal(parseLearnSuggestion({ topic: '   ' }), null)
  assert.equal(parseLearnSuggestion({ topic: null }), null)
})

test('kind 缺失或写了别的值 → 归一成 topic', () => {
  for (const kind of [undefined, null, '', '乱写', 'TOPIC']) {
    const got = parseLearnSuggestion({ topic: 'Java 线程', kind })
    assert.ok(got, `kind=${JSON.stringify(kind)} 不该整条作废`)
    assert.equal(got.kind, 'topic')
  }
})

test('kind 带空格要被救回来（与后端同一规矩）', () => {
  const got = parseLearnSuggestion({ topic: '第三章', kind: '  material  ' })
  assert.ok(got)
  assert.equal(got.kind, 'material')
})

test('topic 被 trim，且解析出的形状能直接喂给卡片', () => {
  const parsed = parseLearnSuggestion({ topic: '  Java 线程  ', kind: 'topic' })
  assert.ok(parsed)
  const card = learnProposalCard(parsed)
  assert.ok(card)
  assert.equal(card.title, '想学「Java 线程」？')
})
