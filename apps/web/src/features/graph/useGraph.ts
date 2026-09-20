/**
 * 图谱视图的取数逻辑。
 *
 * 职责边界：
 *   - 文档列表：一次性拉取，图谱视图不需要上传与轮询进度，所以不复用 useDocuments
 *   - 图谱数据：随选中文档变化重新拉取
 *   - 校验进度：投递后轮询，完成后自动刷新图谱（校验会改变节点颜色）
 *   - 节点详情：选中节点时懒加载详情 + 校验收据 + 关系
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  buildRelations,
  getGraph,
  getKnowledgeChecks,
  getPointRelations,
  getVerifyCapabilities,
  getVerifyStatus,
  startVerify,
  type GraphResponse,
  type KnowledgeCheckResponse,
  type PointRelationItem,
  type VerifyCapabilities,
  type VerifyStatusResponse,
} from '../../api/graph'
import {
  getKnowledgePoint,
  listDocuments,
  type DocumentSummary,
  type KnowledgePointDetail,
} from '../../api/library'
import { messageOf } from '../../api/http'

/** 校验进行中的状态，需要继续轮询 */
const RUNNING_STATES = new Set(['queued', 'running'])

export function useGraph() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [documentId, setDocumentId] = useState<number | null>(null)
  const [graph, setGraph] = useState<GraphResponse | null>(null)
  const [capabilities, setCapabilities] = useState<VerifyCapabilities | null>(null)
  const [verifyStatus, setVerifyStatus] = useState<VerifyStatusResponse | null>(null)

  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [point, setPoint] = useState<KnowledgePointDetail | null>(null)
  const [checks, setChecks] = useState<KnowledgeCheckResponse | null>(null)
  const [relations, setRelations] = useState<PointRelationItem[] | null>(null)
  const [pointLoading, setPointLoading] = useState(false)

  const timerRef = useRef<number | null>(null)

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  // ------------------------------------------------------------ 文档列表
  const loadDocuments = useCallback(async () => {
    try {
      const data = await listDocuments(100, 0)
      setDocuments(data.items)
    } catch (err) {
      setError(messageOf(err))
    }
  }, [])

  useEffect(() => {
    void loadDocuments()
    getVerifyCapabilities()
      .then(setCapabilities)
      .catch(() => setCapabilities(null))
    return stopPolling
  }, [loadDocuments, stopPolling])

  // ------------------------------------------------------------ 图谱数据
  const loadGraph = useCallback(
    async (id: number) => {
      setLoading(true)
      setError(null)
      try {
        const data = await getGraph(id)
        setGraph(data)
        // 选中的节点若已不存在（重建关系或重新解析后），清掉选中态
        setSelectedId((current) =>
          current !== null && !data.nodes.some((n) => n.id === current) ? null : current,
        )
      } catch (err) {
        setError(messageOf(err))
        setGraph(null)
      } finally {
        setLoading(false)
      }
    },
    [],
  )

  useEffect(() => {
    if (documentId === null) {
      setGraph(null)
      return
    }
    void loadGraph(documentId)
    getVerifyStatus(documentId)
      .then(setVerifyStatus)
      .catch(() => setVerifyStatus(null))
  }, [documentId, loadGraph])

  // 默认选中第一个有知识点的文档
  useEffect(() => {
    if (documentId !== null || documents.length === 0) return
    const first = documents.find((d) => d.kp_count > 0) ?? documents[0]
    setDocumentId(first.id)
  }, [documents, documentId])

  // ------------------------------------------------------------ 节点详情
  useEffect(() => {
    if (selectedId === null) {
      setPoint(null)
      setChecks(null)
      setRelations(null)
      return
    }
    let cancelled = false
    setPointLoading(true)
    Promise.all([
      getKnowledgePoint(selectedId),
      getKnowledgeChecks(selectedId).catch(() => null),
      getPointRelations(selectedId).catch(() => null),
    ])
      .then(([detail, checkData, relationData]) => {
        if (cancelled) return
        setPoint(detail)
        setChecks(checkData)
        setRelations(relationData?.items ?? null)
      })
      .catch(async (err) => {
        if (cancelled) return
        setError(messageOf(err))
      })
      .finally(() => {
        if (!cancelled) setPointLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedId])

  // ------------------------------------------------------------ 轮询校验
  const pollVerify = useCallback(
    (id: number) => {
      stopPolling()
      const tick = async () => {
        try {
          const status = await getVerifyStatus(id)
          setVerifyStatus(status)
          if (RUNNING_STATES.has(status.state)) {
            timerRef.current = window.setTimeout(tick, 1500)
            return
          }
          timerRef.current = null
          // 校验完成后刷新图谱：节点的校验状态色要跟着更新
          await loadGraph(id)
          // 若正看着某个节点的详情，也刷新它的校验收据
          setSelectedId((current) => {
            if (current !== null) {
              void Promise.all([
                getKnowledgeChecks(current).then(setChecks).catch(() => undefined),
                getKnowledgePoint(current).then(setPoint).catch(() => undefined),
              ])
            }
            return current
          })
          if (status.state === 'done') setNotice('核对完成，颜色已经按结果更新了。')
        } catch {
          timerRef.current = null
        }
      }
      void tick()
    },
    [loadGraph, stopPolling],
  )

  // ---------------------------------------------------------------- 动作
  const rebuild = useCallback(async () => {
    if (documentId === null) return
    setBusy(true)
    setError(null)
    try {
      const result = await buildRelations(documentId)
      setNotice(result.message)
      await loadGraph(documentId)
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }, [documentId, loadGraph])

  const runVerify = useCallback(async () => {
    if (documentId === null) return
    setBusy(true)
    setError(null)
    try {
      const result = await startVerify(documentId)
      setNotice(result.message)
      pollVerify(documentId)
    } catch (err) {
      setError(messageOf(err))
    } finally {
      setBusy(false)
    }
  }, [documentId, pollVerify])

  const selectNode = useCallback((id: number) => {
    setSelectedId((current) => (current === id ? null : id))
  }, [])

  return {
    documents,
    documentId,
    setDocumentId,
    graph,
    capabilities,
    verifyStatus,
    loading,
    busy,
    error,
    notice,
    setNotice,
    setError,
    selectedId,
    selectNode,
    point,
    checks,
    relations,
    pointLoading,
    rebuild,
    runVerify,
    reload: () => documentId !== null && loadGraph(documentId),
  }
}
