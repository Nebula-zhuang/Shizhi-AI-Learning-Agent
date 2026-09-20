import { useCallback, useEffect, useState } from 'react'

import { StatusBadge } from '../../components/StatusBadge'
import { friendlyWarnings } from '../learn/voice'
import {
  IN_PROGRESS,
  formatBytes,
  getDocument,
  getKnowledgePoint,
  listKnowledgePoints,
  type DocumentDetail as DocumentDetailData,
  type DocumentSummary,
  type KnowledgePointDetail,
  type KnowledgePointSummary,
} from '../../api/library'
import { KnowledgePointCard } from '../knowledge/KnowledgePointCard'
import { SourceViewer } from '../knowledge/SourceViewer'

type OrderKey = 'document' | 'difficulty' | 'importance'

const ORDER_LABELS: Record<OrderKey, string> = {
  document: '按原文顺序',
  difficulty: '按难度',
  importance: '按重要度',
}

interface DocumentDetailProps {
  doc: DocumentSummary
  onReprocess: (id: number) => Promise<unknown>
  onDelete: (id: number) => Promise<unknown>
}

/**
 * 文档详情：统计 / 警告 / 知识点列表 / 来源原文。
 *
 * 刷新时机不是自己轮询，而是跟随父级列表的状态变化 ——
 * 父级已经在轮询，这里再开一套会让同一份数据被请求两次。
 */
export function DocumentDetail({ doc, onReprocess, onDelete }: DocumentDetailProps) {
  const [detail, setDetail] = useState<DocumentDetailData | null>(null)
  const [points, setPoints] = useState<KnowledgePointSummary[]>([])
  const [order, setOrder] = useState<OrderKey>('document')
  const [selected, setSelected] = useState<KnowledgePointDetail | null>(null)
  const [selectedLoading, setSelectedLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const running = IN_PROGRESS.includes(doc.parse_status)

  // ---------------------------------------------------------------- 载入数据
  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        const [info, list] = await Promise.all([
          getDocument(doc.id),
          listKnowledgePoints(doc.id, { order }),
        ])
        if (cancelled) return
        setDetail(info)
        setPoints(list.items)
        setError(null)
      } catch (err) {
        if (!cancelled) setError((err as Error).message)
      }
    })()
    return () => {
      cancelled = true
    }
    // doc.parse_status 变化说明后台处理有进展，需要重新拉取
  }, [doc.id, doc.parse_status, order])

  // 切换文档时清空右侧详情
  useEffect(() => {
    setSelected(null)
  }, [doc.id])

  const openPoint = useCallback(async (id: number) => {
    setSelectedLoading(true)
    try {
      setSelected(await getKnowledgePoint(id))
      setError(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setSelectedLoading(false)
    }
  }, [])

  const handleReprocess = async () => {
    setBusy(true)
    try {
      await onReprocess(doc.id)
      setSelected(null)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!window.confirm(`确定删除《${doc.file_name}》及其全部知识点？此操作不可撤销。`)) {
      return
    }
    setBusy(true)
    try {
      await onDelete(doc.id)
    } catch (err) {
      setError((err as Error).message)
      setBusy(false)
    }
  }

  // 后端的警告偏开发者（含内部阶段名与批次号），先过一层翻译再展示
  const friendlyNotices = friendlyWarnings(
    (detail?.warnings ?? []) as { code?: string; message: string; page_no?: number | null }[],
  )

  return (
    <section className="flex h-full min-h-0 flex-col gap-3">
      {/* ------------------------------------------------------------ 头部 */}
      <header className="rounded-xl border border-line bg-paper-raised px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate text-sm font-medium text-ink-1" title={doc.file_name}>
                {doc.file_name}
              </h2>
              <StatusBadge status={doc.parse_status} />
            </div>
            <p className="mt-1 text-xs text-ink-3">
              {/* 只报学习者在意的：多厚、多少图、多大。
                  "字符数 / 文本块数"是索引层的概念，对学习者没有意义，去掉了。 */}
              {doc.page_count > 0 && <span>{doc.page_count} 页 · </span>}
              {doc.image_count > 0 && <span>{doc.image_count} 张图片 · </span>}
              <span>{formatBytes(doc.file_size)}</span>
            </p>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              onClick={() => void handleReprocess()}
              disabled={busy || running}
              className="rounded-md border border-line px-3 py-1.5 text-xs text-ink-2 transition hover:bg-paper-sunken disabled:cursor-not-allowed disabled:opacity-40"
            >
              重新处理
            </button>
            <button
              type="button"
              onClick={() => void handleDelete()}
              disabled={busy || running}
              className="rounded-md border border-brick-line px-3 py-1.5 text-xs text-brick transition hover:bg-brick-soft disabled:cursor-not-allowed disabled:opacity-40"
            >
              删除
            </button>
          </div>
        </div>

        {running && (
          <div className="mt-3">
            <div className="h-1 w-full overflow-hidden rounded-full bg-paper-sunken">
              <div
                className="h-full rounded-full bg-moss transition-all duration-500"
                style={{ width: `${Math.max(doc.progress, 3)}%` }}
              />
            </div>
            <p className="mt-1 text-xs text-ink-4">
              {doc.stage_detail}（{doc.progress}%）
            </p>
          </div>
        )}
      </header>

      {/* -------------------------------------------------------- 错误与警告 */}
      {error && (
        <div className="rounded-lg border border-brick-line bg-brick-soft px-4 py-2.5 text-xs text-brick-ink">
          {error}
        </div>
      )}

      {detail?.parse_error && (
        <div className="rounded-lg border border-brick-line bg-brick-soft px-4 py-2.5">
          <p className="text-xs font-medium text-brick-ink">处理失败</p>
          <p className="mt-1 text-xs leading-relaxed text-brick">{detail.parse_error}</p>
        </div>
      )}

      {friendlyNotices.length > 0 && (
        <details className="rounded-lg border border-sienna-line bg-sienna-soft px-4 py-2.5">
          <summary className="cursor-pointer text-xs font-medium text-sienna-ink">
            我读的时候遇到的情况（{friendlyNotices.length} 条）
          </summary>
          <ul className="mt-2 list-disc space-y-1 pl-4">
            {friendlyNotices.map((notice, index) => (
              <li key={index} className="text-xs leading-relaxed text-sienna-ink">
                {notice}
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* -------------------------------------------------------- 知识点区 */}
      {points.length === 0 ? (
        <div className="flex flex-1 items-center justify-center rounded-xl border border-line bg-paper-raised">
          <p className="text-xs text-ink-4">
            {running ? '正在抽取知识点…' : '该文档还没有知识点'}
          </p>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 gap-3">
          <div className="flex min-h-0 flex-1 flex-col rounded-xl border border-line bg-paper-raised">
            <div className="flex items-center justify-between border-b border-line px-4 py-2.5">
              <p className="text-xs font-medium text-ink-2">
                知识点 {points.length} 个
              </p>
              <div className="flex items-center gap-1">
                {(Object.keys(ORDER_LABELS) as OrderKey[]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    onClick={() => setOrder(key)}
                    className={[
                      'rounded px-2 py-1 text-xs transition',
                      order === key
                        ? 'bg-moss-soft text-moss'
                        : 'text-ink-4 hover:bg-paper-sunken hover:text-ink-2',
                    ].join(' ')}
                  >
                    {ORDER_LABELS[key]}
                  </button>
                ))}
              </div>
            </div>

            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-3 py-3">
              {points.map((point) => (
                <KnowledgePointCard
                  key={point.id}
                  point={point}
                  selected={selected?.id === point.id}
                  onSelect={(id) => void openPoint(id)}
                />
              ))}
            </div>
          </div>

          {selected && (
            <div className="hidden min-h-0 w-[380px] shrink-0 lg:block">
              <SourceViewer
                point={selected}
                loading={selectedLoading}
                error={null}
                onClose={() => setSelected(null)}
              />
            </div>
          )}
        </div>
      )}
    </section>
  )
}
