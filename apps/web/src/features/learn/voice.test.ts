/**
 * `humanizeRecall` —— 把后端正文开头那句排障文案换成用户能懂的说法。
 *
 * ## 守什么
 *
 * 后端 `memory_service.build_recall_note` 产出的是「……作答 3 次，**掌握度 0.00**……」——
 * 那是**排障口径**，有意保留（后端文案不动 ✓）。但它会被 `runtime.py` 拼进正文最前面，
 * 模型还会原样复述，于是直接进了用户视野 ✗
 * 这一页的规矩是：**掌握度只说人话，不给数字** ✓
 *
 * 另外两条同等重要：
 * - **没文案就不产生任何额外内容** ✓
 * - **对不上就原样返回** ✓ —— 不能把正文改坏
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { humanizeRecall, recallSentence } from './voice.ts'

/** 后端真实拼出来的那种正文：recall_note + 分隔线 + 教学内容 */
const RAW_RECALL =
  '你上次学过这个知识点，作答 4 次，掌握度 0.35。我们先把上次没通的地方捡起来，再往下走。'
const TEACHING = '破坏循环等待，是通过统一所有线程申请资源的顺序，来彻底消除死锁四条件之一。'

function content(prefix = RAW_RECALL): string {
  return `${prefix}\n\n---\n\n${TEACHING}`
}

const MEMORY = {
  knowledge_state: {
    mastery: 0.35,
    attempt_count: 4,
    consecutive_wrong: 0,
    last_error_type: null,
  },
  is_due: false,
  recall_note: RAW_RECALL,
}

// --------------------------------------------------------------------------- //
// 一、核心：原始 mastery 数字不再出现在用户文案里
// --------------------------------------------------------------------------- //
test('⚠️ 正文里的「掌握度 0.35」被换成说法，原始数字消失', () => {
  const out = humanizeRecall(content(), MEMORY)

  assert.ok(!out.includes('0.35'), '原始 mastery 数字还在')
  assert.ok(!out.includes('掌握度'), '“掌握度”这个数据库口径还在')
  assert.ok(out.includes('你上次'), '换上的句子仍要说“上次”')
})

test('掌握度是 0.00 时同样不出现数字', () => {
  const raw = '你上次学过这个知识点，作答 3 次，掌握度 0.00。结束时连续答错 3 次。'
  const out = humanizeRecall(content(raw), {
    knowledge_state: { mastery: 0, attempt_count: 3, consecutive_wrong: 3, last_error_type: 'memory_gap' },
    is_due: false,
    recall_note: raw,
  })

  assert.ok(!out.includes('0.00'), '0.00 也是数字，不该出现')
  assert.ok(!out.includes('掌握度'))
})

test('替换后教学内容与分隔线原样保留', () => {
  const out = humanizeRecall(content(), MEMORY)

  assert.ok(out.includes('---'), '分隔线被吃掉了')
  assert.ok(out.includes(TEACHING), '教学内容被改动了')
  assert.ok(out.endsWith(TEACHING), '教学内容应当仍在结尾')
})

test('连错时给人的是「连错 N 次」，不是小数', () => {
  const raw = '你上次学过这个知识点，作答 9 次，掌握度 0.12。'
  const out = humanizeRecall(content(raw), {
    knowledge_state: { mastery: 0.12, attempt_count: 9, consecutive_wrong: 4, last_error_type: 'concept_confusion' },
    is_due: false,
    recall_note: raw,
  })

  assert.ok(!out.includes('0.12'))
  assert.ok(out.includes('连错 4 次'), '应当说清连错几次（这是人话，可以留）')
})

test('⚠️ 不会出现「没答到点上上」这种重复尾缀', () => {
  // `ERROR_WORDS.none` 本身就是「没答到点上」，末尾已经带「上」——
  // 模板再加一个「上」就会变成「没答到点上上」✗
  // （`lastTrouble` 用的是不带尾缀的写法，这里与它对齐 ✓）
  const raw = '你上次学过这个知识点，作答 5 次，掌握度 0.57。'
  const out = humanizeRecall(content(raw), {
    knowledge_state: { mastery: 0.57, attempt_count: 5, consecutive_wrong: 0, last_error_type: 'none' },
    is_due: false,
    recall_note: raw,
  })

  assert.ok(!out.includes('上上'), `出现了重复尾缀：${out.slice(0, 60)}`)
  assert.ok(out.includes('没答到点上'), '该说的还得说')
})

test('连错分支同样不出现重复尾缀', () => {
  const raw = '你上次学过这个知识点，作答 5 次，掌握度 0.20。'
  const out = humanizeRecall(content(raw), {
    knowledge_state: { mastery: 0.2, attempt_count: 5, consecutive_wrong: 3, last_error_type: 'none' },
    is_due: false,
    recall_note: raw,
  })

  assert.ok(!out.includes('上上'), `出现了重复尾缀：${out.slice(0, 60)}`)
  assert.ok(out.includes('连错 3 次'))
})

// --------------------------------------------------------------------------- //
// 二、没文案 → 不产生额外内容
// --------------------------------------------------------------------------- //
test('没有 recall_note 时正文一字不动', () => {
  const plain = TEACHING
  assert.equal(humanizeRecall(plain, { knowledge_state: null, is_due: false }), plain)
  assert.equal(humanizeRecall(plain, {}), plain)
  assert.equal(humanizeRecall(plain, null), plain)
  assert.equal(humanizeRecall(plain, undefined), plain)
  assert.equal(humanizeRecall(plain), plain)
})

test('recall_note 是空串 / 全空白时也算没有', () => {
  assert.equal(humanizeRecall(TEACHING, { recall_note: '' }), TEACHING)
  assert.equal(humanizeRecall(TEACHING, { recall_note: '   ' }), TEACHING)
})

// --------------------------------------------------------------------------- //
// 三、对不上 → 原样返回（不许把正文改坏）
// --------------------------------------------------------------------------- //
test('正文不是以那句 recall_note 开头时不动它', () => {
  const other = `${TEACHING}\n\n${RAW_RECALL}` // 夹在中间，不是开头
  assert.equal(humanizeRecall(other, MEMORY), other)
})

test('recall_note 与正文对不上（后端文案变了）时不动它', () => {
  const stale = '一句和正文毫无关系的话'
  const c = content()
  assert.equal(humanizeRecall(c, { ...MEMORY, recall_note: stale }), c)
})

test('拿不到结构化状态时退回后端原文（宁可难看，不能不说）', () => {
  const c = content()
  const out = humanizeRecall(c, { recall_note: RAW_RECALL })

  assert.equal(out, c, '没有 knowledge_state 时应当原样返回 recall_note 版本')
})

// --------------------------------------------------------------------------- //
// 四、普通教学正文完全不受影响
// --------------------------------------------------------------------------- //
test('普通教学正文（无 recall 前缀）不受影响', () => {
  const plain = '线程是程序执行的最小单位，是 CPU 调度的基本单元。'
  assert.equal(humanizeRecall(plain, MEMORY), plain)
})

test('已经换过一次的不再被二次处理（幂等）', () => {
  const once = humanizeRecall(content(), MEMORY)
  assert.equal(humanizeRecall(once, MEMORY), once, '第二次不该再变')
})

test('空正文不炸', () => {
  assert.equal(humanizeRecall('', MEMORY), '')
  assert.equal(humanizeRecall('', undefined), '')
})

// --------------------------------------------------------------------------- //
// 五、与 recallSentence 的关系：换上的句子就是它产出的
// --------------------------------------------------------------------------- //
test('换上的句子确实来自 recallSentence（同一批真实字段）', () => {
  const out = humanizeRecall(content(), MEMORY)
  const expected = recallSentence({
    knowledge_state: MEMORY.knowledge_state,
    is_due: MEMORY.is_due,
    recall_note: MEMORY.recall_note,
  })

  assert.ok(out.startsWith(expected), '开头应当就是 recallSentence 的结果')
})
