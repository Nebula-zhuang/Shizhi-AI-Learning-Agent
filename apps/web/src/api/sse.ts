/**
 * SSE 帧解析（与传输层解耦的纯逻辑）。
 *
 * 单独成文件的原因：
 * 1. 不依赖 import.meta.env 等构建期变量，因此可以被 Node 直接加载测试；
 * 2. TCP 分片是随机的 —— 一个帧可能被切成任意多段，必须用「缓冲 + 边界扫描」
 *    的方式增量解析，这也是最容易出 bug 的地方，需要独立覆盖。
 */

export interface SseFrame {
  event: string
  data: unknown
}

/** 帧分隔符：空行（兼容 \n\n 与 \r\n\r\n） */
const FRAME_BOUNDARY = /\r?\n\r?\n/

/**
 * 解析单个 SSE 帧文本，形如：
 *     event: delta
 *     data: {"text":"hi"}
 * 返回 null 表示该帧无有效数据（例如仅注释或心跳）。
 */
export function parseSseFrame(raw: string): SseFrame | null {
  let event = 'message'
  const dataLines: string[] = []

  for (const line of raw.split('\n')) {
    const trimmed = line.endsWith('\r') ? line.slice(0, -1) : line
    if (trimmed.startsWith(':')) continue // 注释帧 / 心跳
    if (trimmed.startsWith('event:')) {
      event = trimmed.slice(6).trim()
    } else if (trimmed.startsWith('data:')) {
      dataLines.push(trimmed.slice(5).trim())
    }
  }

  if (dataLines.length === 0) return null

  try {
    return { event, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null // 非法 JSON 直接丢弃，避免一个坏帧打断整条流
  }
}

/**
 * 增量帧解码器。
 *
 * 用法：把每次 read() 得到的文本片段 push 进去，返回本次能拼出的完整帧。
 * 内部保留未完整的尾部，因此不要求调用方按帧切分。
 */
export class SseDecoder {
  private buffer = ''

  push(chunk: string): SseFrame[] {
    this.buffer += chunk
    const frames: SseFrame[] = []

    for (;;) {
      const match = FRAME_BOUNDARY.exec(this.buffer)
      if (!match || match.index === undefined) break

      const raw = this.buffer.slice(0, match.index)
      this.buffer = this.buffer.slice(match.index + match[0].length)

      const frame = parseSseFrame(raw)
      if (frame) frames.push(frame)
    }

    return frames
  }

  /** 流结束时调用，处理没有以空行收尾的残留帧 */
  flush(): SseFrame[] {
    const rest = this.buffer
    this.buffer = ''
    if (!rest.trim()) return []
    const frame = parseSseFrame(rest)
    return frame ? [frame] : []
  }
}
