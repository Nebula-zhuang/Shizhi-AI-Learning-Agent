/**
 * SSE 解析器单元测试。
 *
 * 为什么必须有这组测试：
 * TCP 分片位置是随机的，一个 SSE 帧可能被切成任意多段。如果解析器只在
 * 「一个 chunk 恰好等于一个帧」时能工作，真实浏览器里就会偶发丢帧或花屏，
 * 而且很难复现。这里用「穷举切分点」的方式把这种偶发性变成确定性验证。
 *
 * 运行：
 *     npm run test:unit
 * 或：
 *     node --experimental-strip-types --test src/api/sse.test.ts
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseSseFrame, SseDecoder } from './sse.ts'

/** 构造与后端 app/api/sse.py 完全一致的帧 */
function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`
}

const META = frame('meta', { model: 'deepseek-chat', mode: 'mock', request_id: 'abc123' })
const DELTA1 = frame('delta', { text: '你好，' })
const DELTA2 = frame('delta', { text: '这是流式输出。' })
const DONE = frame('done', { request_id: 'abc123', chunks: 2, chars: 9, elapsed_ms: 42 })

const STREAM = META + DELTA1 + DELTA2 + DONE

/** 把字符串按固定长度切片，模拟网络分块 */
function chunkize(text: string, size: number): string[] {
  const out: string[] = []
  for (let i = 0; i < text.length; i += size) {
    out.push(text.slice(i, i + size))
  }
  return out
}

interface Collected {
  events: string[]
  text: string
  meta: unknown
  done: unknown
}

function feed(chunks: string[]): Collected {
  const decoder = new SseDecoder()
  const result: Collected = { events: [], text: '', meta: null, done: null }

  const handle = (f: { event: string; data: unknown }) => {
    result.events.push(f.event)
    if (f.event === 'delta') {
      result.text += (f.data as { text: string }).text
    } else if (f.event === 'meta') {
      result.meta = f.data
    } else if (f.event === 'done') {
      result.done = f.data
    }
  }

  for (const chunk of chunks) {
    for (const f of decoder.push(chunk)) handle(f)
  }
  for (const f of decoder.flush()) handle(f)

  return result
}

test('parseSseFrame 解析标准帧', () => {
  const parsed = parseSseFrame('event: delta\ndata: {"text":"hi"}')
  assert.deepEqual(parsed, { event: 'delta', data: { text: 'hi' } })
})

test('parseSseFrame 忽略注释帧与心跳', () => {
  assert.equal(parseSseFrame(': keep-alive'), null)
  assert.equal(parseSseFrame(''), null)
})

test('parseSseFrame 对非法 JSON 返回 null 而不是抛错', () => {
  assert.equal(parseSseFrame('event: delta\ndata: {oops'), null)
})

test('parseSseFrame 支持多行 data 拼接', () => {
  const parsed = parseSseFrame('event: x\ndata: {"a":1,\ndata: "b":2}')
  assert.deepEqual(parsed?.data, { a: 1, b: 2 })
})

test('SseDecoder 兼容 \\r\\n 分隔符', () => {
  const payload = 'event: delta\r\ndata: {"text":"x"}\r\n\r\n'
  const collector = feed([payload])
  assert.deepEqual(collector.events, ['delta'])
  assert.equal(collector.text, 'x')
})

test('一个 chunk 内含多帧时全部解出', () => {
  const collector = feed([STREAM])
  assert.deepEqual(collector.events, ['meta', 'delta', 'delta', 'done'])
  assert.equal(collector.text, '你好，这是流式输出。')
})

test('穷举切分点：任意分片方式都必须还原出相同结果', () => {
  // 从 1 字节一片到整段一片，逐一验证
  for (let size = 1; size <= STREAM.length; size++) {
    const collector = feed(chunkize(STREAM, size))
    assert.deepEqual(
      collector.events,
      ['meta', 'delta', 'delta', 'done'],
      `分片大小 ${size} 时事件序列错误`,
    )
    assert.equal(collector.text, '你好，这是流式输出。', `分片大小 ${size} 时文本错误`)
  }
})

test('穷举单个切分点：在每一个位置断开都成立', () => {
  // 只切一刀的情况，覆盖「帧边界恰好落在 read 边界上」的高危场景
  for (let cut = 0; cut <= STREAM.length; cut++) {
    const collector = feed([STREAM.slice(0, cut), STREAM.slice(cut)])
    assert.deepEqual(collector.events, ['meta', 'delta', 'delta', 'done'], `切点 ${cut} 失败`)
    assert.equal(collector.text, '你好，这是流式输出。', `切点 ${cut} 文本错误`)
  }
})

test('flush 能处理服务端未以空行收尾的残留帧', () => {
  const collector = feed(['event: delta\ndata: {"text":"tail"}'])
  assert.deepEqual(collector.events, ['delta'])
  assert.equal(collector.text, 'tail')
})

test('meta 与 done 的内容被完整保留', () => {
  const collector = feed(chunkize(STREAM, 3))
  assert.deepEqual(collector.meta, {
    model: 'deepseek-chat',
    mode: 'mock',
    request_id: 'abc123',
  })
  assert.deepEqual(collector.done, {
    request_id: 'abc123',
    chunks: 2,
    chars: 9,
    elapsed_ms: 42,
  })
})

test('中文与换行在分片后不出现乱码', () => {
  // TextDecoder 的多字节边界处理由调用方负责，这里验证纯文本层面的完整性
  const text = '第一行\n第二行 ✅ 结束'
  const collector = feed(chunkize(frame('delta', { text }), 1))
  assert.equal(collector.text, text)
})
