/**
 * 知识点选择器。
 *
 * ## 一个组件两种形态
 *
 *   · **嵌入式**（`embedded`）：没在学时，「辅导」页直接把可学的知识点铺开 ——
 *     这样这一页永远有内容，而不是一个"请先选择"的空框。
 *   · **弹层式**：学习中途想换一个时用，不打断当前对话的上下文。
 *
 * ## 为什么按资料 + 章节分组
 *
 * 知识点是散的两百多个，平铺出来没人能选。按"资料 → 章节"收成两层之后，
 * 学习者能顺着"我上次读的是哪章"找回去 —— 这正是人回忆知识的方式。
 */

import { useEffect, useMemo, useState } from 'react'
import { motion } from 'motion/react'

import { getTutorDashboard } from '../../api/tutor'
import {
  listDocuments,
  listKnowledgePoints,
  type KnowledgePointSummary,
} from '../../api/library'
import { messageOf } from '../../api/http'
import { useLearning } from '../../app/LearningProvider'
import {
  Button,
  EmptyState,
  ErrorState,
  IconArrowRight,
  IconBookmark,
  IconFile,
  IconSearch,
  Modal,
  Skeleton,
  TONE_DOT,
  cn,
} from '../../ui'
import type { Tone } from '../../ui'
import { lastTrouble, masteryMark } from './voice'

/**
 * 列表行。
 *
 * 知识点摘要接口**不带学习状态**（那是 learner 维度的数据），
 * 所以掌握度要从看板的 `weak_points` 合并进来 —— 合并不到就是"还没学过"。
 * 这比"给每个知识点单独发一次请求"省几十次往返。
 */
interface Row extends KnowledgePointSummary {
  documentName: string
  /** 章节路径；没有就落到"未分章" */
  chapter: string
  mastery: number
  attemptCount: number
  consecutiveWrong: number
  lastErrorType: string | null
}

function useKnowledgeIndex() {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const [docs, dashboard] = await Promise.all([listDocuments(100), getTutorDashboard()])
        const ready = docs.items.filter((doc) => doc.kp_count > 0)

        // 掌握度按知识点 id 建索引，供合并
        const stateOf = new Map(
          (dashboard?.weak_points ?? []).map((item) => [item.knowledge_point_id, item]),
        )

        const collected: Row[] = []
        for (const doc of ready) {
          const page = await listKnowledgePoints(doc.id, { limit: 500 })
          for (const point of page.items) {
            const state = stateOf.get(point.id)
            collected.push({
              ...point,
              documentName: doc.file_name,
              chapter: point.heading_path?.filter(Boolean).join(' / ') || '未分章',
              mastery: state?.mastery ?? 0,
              attemptCount: state?.attempt_count ?? 0,
              consecutiveWrong: state?.consecutive_wrong ?? 0,
              lastErrorType: state?.last_error_type ?? null,
            })
          }
        }
        if (alive) setRows(collected)
      } catch (err) {
        if (alive) setError(messageOf(err))
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => {
      alive = false
    }
  }, [])

  return { rows, loading, error }
}

/** 一行知识点 */
function PointRow({
  row,
  index,
  onPick,
}: {
  row: Row
  index: number
  onPick: () => void
}) {
  const untouched = row.attemptCount === 0
  const tone: Tone = untouched
    ? 'neutral'
    : row.mastery >= 0.7
      ? 'moss'
      : row.mastery > 0.2
        ? 'sienna'
        : 'brick'

  return (
    <motion.button
      type="button"
      onClick={onPick}
      initial={{ opacity: 0, y: 5 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, delay: Math.min(index * 0.025, 0.3), ease: [0.16, 1, 0.3, 1] }}
      className="group flex w-full items-center gap-3 rounded-md px-3 py-2.5 text-left transition-colors duration-150 hover:bg-paper-sunken"
    >
      <span
        aria-hidden="true"
        className={cn('h-1.5 w-1.5 shrink-0 rounded-full', TONE_DOT[tone])}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm text-ink-1">{row.title}</span>
        <span className="mt-0.5 block truncate text-2xs text-ink-3">
          {untouched
            ? '还没学过'
            : `${masteryMark({ mastery: row.mastery, attempt_count: row.attemptCount })} · ${lastTrouble(
                {
                  attempt_count: row.attemptCount,
                  consecutive_wrong: row.consecutiveWrong,
                  last_error_type: row.lastErrorType,
                },
              )}`}
        </span>
      </span>
      <span className="shrink-0 text-ink-4 opacity-0 transition-opacity duration-200 group-hover:opacity-100">
        <IconArrowRight size={14} />
      </span>
    </motion.button>
  )
}

export function PointPicker({
  open,
  onClose,
  embedded = false,
}: {
  open: boolean
  onClose: () => void
  embedded?: boolean
}) {
  const { startLearning, focus } = useLearning()
  const { rows, loading, error } = useKnowledgeIndex()
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return rows
    return rows.filter(
      (row) =>
        row.title.toLowerCase().includes(keyword) ||
        row.documentName.toLowerCase().includes(keyword) ||
        row.chapter.toLowerCase().includes(keyword),
    )
  }, [rows, query])

  // 按「资料 → 章节」两级分组
  const grouped = useMemo(() => {
    const byDoc = new Map<string, Map<string, Row[]>>()
    for (const row of filtered) {
      if (!byDoc.has(row.documentName)) byDoc.set(row.documentName, new Map())
      const byChapter = byDoc.get(row.documentName)!
      if (!byChapter.has(row.chapter)) byChapter.set(row.chapter, [])
      byChapter.get(row.chapter)!.push(row)
    }
    return [...byDoc.entries()]
  }, [filtered])

  const body = (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* 搜索 */}
      <div className="relative mb-4">
        <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-4">
          <IconSearch size={14} />
        </span>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="找知识点、章节或资料名"
          className="field pl-9 text-sm"
          aria-label="搜索知识点"
        />
      </div>

      {loading && (
        <div className="space-y-2.5">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="flex items-center gap-3 px-3">
              <Skeleton width={6} height={6} className="rounded-full" />
              <div className="flex-1 space-y-1.5">
                <Skeleton width="52%" height={13} />
                <Skeleton width="34%" height={11} />
              </div>
            </div>
          ))}
        </div>
      )}

      {error && <ErrorState message={error} />}

      {!loading && !error && filtered.length === 0 && (
        <EmptyState
          icon={<IconFile size={20} />}
          title={rows.length === 0 ? '还没有可以学的知识点' : '没找到匹配的'}
          description={
            rows.length === 0
              ? '先到「资料」页传一份讲义或教材，我读完会把它拆成知识点。'
              : '换个说法再找找，或者清空搜索看全部。'
          }
          action={
            rows.length > 0 ? (
              <Button variant="quiet" size="sm" onClick={() => setQuery('')}>
                清空搜索
              </Button>
            ) : undefined
          }
        />
      )}

      {/* 分组列表 */}
      {!loading && filtered.length > 0 && (
        <div className={cn('space-y-6', embedded ? '' : 'min-h-0 flex-1 overflow-y-auto pr-1')}>
          {grouped.map(([documentName, chapters], docIndex) => (
            <section key={documentName}>
              <div className="mb-2 flex items-center gap-2">
                <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-moss" />
                <h3 className="truncate text-xs font-medium text-ink-2">{documentName}</h3>
              </div>

              <div className="space-y-3 pl-3.5">
                {[...chapters.entries()].map(([chapter, items]) => (
                  <div key={chapter}>
                    <p className="mb-1 flex items-center gap-1.5 text-2xs text-ink-4">
                      <IconBookmark size={11} />
                      {chapter}
                    </p>
                    <div className="space-y-0.5">
                      {items.map((row, index) => (
                        <PointRow
                          key={row.id}
                          row={row}
                          index={embedded ? docIndex * 3 + index : index}
                          onPick={() => {
                            void startLearning({
                              kpId: row.id,
                              title: row.title,
                              documentId: row.document_id,
                            })
                            if (!embedded) onClose()
                          }}
                        />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  )

  // 嵌入式：直接铺在页面里
  if (embedded) {
    return (
      <div className="surface p-5">
        <div className="mb-4 flex items-baseline justify-between gap-4">
          <div>
            <p className="meta tracking-[0.12em]">可以学的</p>
            <h2 className="mt-1 text-lg">
              {rows.length > 0 ? `一共 ${rows.length} 个知识点` : '还没有内容'}
            </h2>
          </div>
          {focus && (
            <span className="meta truncate">上次学到「{focus.title}」</span>
          )}
        </div>
        {body}
      </div>
    )
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="换一个知识点"
      description="按资料和章节排好了，也可以直接搜。"
      width="46rem"
    >
      <div className="flex h-[60vh] flex-col">{body}</div>
    </Modal>
  )
}
