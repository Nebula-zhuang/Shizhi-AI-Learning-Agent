import { useCallback, useEffect, useRef, useState } from 'react'

import {
  IN_PROGRESS,
  deleteDocument,
  listDocuments,
  reprocessDocument,
  uploadDocument,
  type DocumentSummary,
  type UploadResponse,
} from '../../api/library'
import type { UploadOptions } from '../../api/http'

/** 轮询间隔。仅在有文档处于中间态时才轮询，全部终态后自动停止。 */
const POLL_INTERVAL_MS = 2000

/**
 * 文档列表状态管理。
 *
 * 轮询策略：只有当列表中存在非终态文档时才持续拉取。
 * 空闲页面持续打接口是常见的实现疏忽，这里刻意避免。
 */
export function useDocuments() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const timer = useRef<number | null>(null)

  const refresh = useCallback(async (options: { silent?: boolean } = {}) => {
    if (!options.silent) setLoading(true)
    try {
      const data = await listDocuments()
      setDocuments(data.items)
      setTotal(data.total)
      setError(null)
      return data.items
    } catch (err) {
      setError((err as Error).message)
      return []
    } finally {
      if (!options.silent) setLoading(false)
    }
  }, [])

  // 轮询：发现中间态就继续，否则停表
  const scheduleIfNeeded = useCallback(
    (items: DocumentSummary[]) => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current)
        timer.current = null
      }
      const running = items.some((d) => IN_PROGRESS.includes(d.parse_status))
      if (!running) return

      timer.current = window.setTimeout(async () => {
        const latest = await refresh({ silent: true })
        scheduleIfNeeded(latest)
      }, POLL_INTERVAL_MS)
    },
    [refresh],
  )

  useEffect(() => {
    void (async () => {
      const items = await refresh()
      scheduleIfNeeded(items)
    })()
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    }
  }, [refresh, scheduleIfNeeded])

  /** 上传成功后刷新列表并（必要时）启动轮询 */
  const upload = useCallback(
    async (file: File, options: UploadOptions = {}): Promise<UploadResponse> => {
      setNotice(null)
      const result = await uploadDocument(file, options)
      setNotice(result.message)
      const items = await refresh({ silent: true })
      scheduleIfNeeded(items)
      return result
    },
    [refresh, scheduleIfNeeded],
  )

  const remove = useCallback(
    async (id: number) => {
      const result = await deleteDocument(id)
      setNotice(result.message)
      await refresh({ silent: true })
    },
    [refresh],
  )

  const reprocess = useCallback(
    async (id: number) => {
      const result = await reprocessDocument(id)
      setNotice(result.message)
      const items = await refresh({ silent: true })
      scheduleIfNeeded(items)
      return result
    },
    [refresh, scheduleIfNeeded],
  )

  return {
    documents,
    total,
    loading,
    error,
    notice,
    setNotice,
    refresh,
    upload,
    remove,
    reprocess,
  }
}
