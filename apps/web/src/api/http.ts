/**
 * 通用 HTTP 辅助。
 *
 * 为什么不复用 api/client.ts：那个文件承载的是 P0 已验证的 SSE 流式客户端，
 * 改动它有回归风险。这里提供资料库需要的普通 JSON 请求能力，两者互不干扰。
 */

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

export function apiUrl(path: string): string {
  return `${API_BASE}${path}`
}

/** 从错误响应里尽力提取可读信息 */
export async function extractError(res: Response): Promise<string> {
  try {
    const text = await res.text()
    try {
      const parsed = JSON.parse(text) as { detail?: unknown }
      if (typeof parsed.detail === 'string') return parsed.detail
      if (Array.isArray(parsed.detail)) {
        // FastAPI 的参数校验错误是数组结构
        return parsed.detail
          .map((d) => (typeof d === 'object' && d && 'msg' in d ? String(d.msg) : String(d)))
          .join('；')
      }
    } catch {
      /* 非 JSON，用原文 */
    }
    return text || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status} ${res.statusText}`
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    // 带上 Cookie：登录态靠 httpOnly Cookie 传递，前端**不持有令牌**，
    // 所以每个请求都必须让浏览器把凭据带上。
    res = await fetch(apiUrl(path), { credentials: 'include', ...init })
  } catch {
    throw new Error('连不上学习伙伴服务，请确认后端已启动。')
  }
  if (!res.ok) {
    throw new Error(await extractError(res))
  }
  if (res.status === 204) {
    return undefined as T
  }
  return (await res.json()) as T
}

export function getJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'GET' })
}

export function postJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function putJson<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function deleteJson<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' })
}

/** 上传文件时的可选回调。 */
export interface UploadOptions {
  /** 上传进度 0–1。**只反映"传到服务端"这一段**，不含服务端解析 */
  onProgress?: (ratio: number) => void
  /** 让调用方能取消（用户点了取消、或切走了页面） */
  signal?: AbortSignal
}

/**
 * 上传文件。
 *
 * ## 为什么这里用 XHR 而不是 fetch
 *
 * 唯一的原因是**上传进度**。`fetch` 至今没有可用的上传进度事件；
 * 文件上限提到 300MB 之后，"发出去到服务端开始处理"之间可能有好几秒到几十秒
 * 完全没有反馈 —— 用户会以为卡死，然后重复点击。
 * XHR 的 `upload.onprogress` 是标准里唯一能拿到这个进度的手段。
 *
 * ## 错误语义与 `request()` 保持一致
 *
 * 同样把响应体里的 `detail` 提取出来抛成 `Error.message`，
 * 这样上层用同一个 `messageOf()` 就能显示人话。
 *
 * 不设 Content-Type —— 交给浏览器自动带 multipart 边界。
 */
export function uploadFile<T>(path: string, file: File, options: UploadOptions = {}): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const form = new FormData()
    form.append('file', file)

    const xhr = new XMLHttpRequest()
    xhr.open('POST', apiUrl(path), true)
    // 登录态在 httpOnly Cookie 里，跨源时必须显式允许携带凭据
    xhr.withCredentials = true

    const onAbort = () => xhr.abort()

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && options.onProgress) {
        options.onProgress(event.loaded / event.total)
      }
    }

    xhr.onload = () => {
      options.signal?.removeEventListener('abort', onAbort)
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as T)
        } catch {
          reject(new Error('服务端返回的内容看不懂，请稍后再试。'))
        }
        return
      }
      // 复用与 fetch 路径同一套提取逻辑，避免两条路径给出两种措辞
      reject(new Error(extractErrorFromText(xhr)))
    }

    xhr.onerror = () => {
      options.signal?.removeEventListener('abort', onAbort)
      reject(new Error('上传过程中断了。可能是后端没起来，或者文件太大被中断。'))
    }

    xhr.ontimeout = () => {
      options.signal?.removeEventListener('abort', onAbort)
      reject(new Error('上传超时了。如果文件很大，检查一下网络。'))
    }

    xhr.onabort = () => {
      options.signal?.removeEventListener('abort', onAbort)
      reject(new Error('__aborted__'))
    }

    if (options.signal) {
      if (options.signal.aborted) {
        reject(new Error('__aborted__'))
        return
      }
      options.signal.addEventListener('abort', onAbort)
    }

    xhr.send(form)
  })
}

/** 从 XHR 的响应里提取可读错误。与 `extractError(Response)` 同样的规则。 */
function extractErrorFromText(xhr: XMLHttpRequest): string {
  const text = xhr.responseText || ''
  try {
    const parsed = JSON.parse(text) as { detail?: unknown }
    if (typeof parsed.detail === 'string') return parsed.detail
    if (Array.isArray(parsed.detail)) {
      return parsed.detail
        .map((d) => (typeof d === 'object' && d && 'msg' in d ? String(d.msg) : String(d)))
        .join('；')
    }
  } catch {
    /* 非 JSON，用原文或状态码 */
  }
  if (xhr.status === 413) return '文件超过了服务端的大小上限。'
  return text || `HTTP ${xhr.status}`
}

/**
 * 把任何抛出物变成一句能给人看的话。
 *
 * **为什么需要它**：`request()` 抛出来的是已经提取好消息的普通 `Error`，
 * 不是 `Response`。如果在上层再把它当 `Response` 解析一遍，
 * `res.status` 会是 undefined，用户看到的就是"（HTTP undefined undefined）"。
 *
 * 这个坑我自己踩过：登录失败时界面上真的显示了那几个字。
 */
export function messageOf(err: unknown): string {
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  return '遇到了一个说不清的问题，稍后再试。'
}
