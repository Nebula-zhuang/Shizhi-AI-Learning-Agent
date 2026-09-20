/**
 * 知识地图视图。
 *
 * 布局：左侧资料与操作，中间画布，右侧知识点详情。
 *
 * 与 Tutor 的联动：当前正在学的知识点在画布上有**高亮环**（来自共享的
 * LearningContext），选中一个节点还能看到「去学这个」的入口 ——
 * 地图不只是"看关系"的，也是"从关系回到学习"的入口。
 */

import { useState } from 'react'

import { RELATION_LABEL, VERIFY_STATUS_LABEL, type RelationType } from '../../api/graph'
import { useLearning } from '../../app/LearningProvider'
import { SourceViewer } from '../knowledge/SourceViewer'
import { GraphCanvas } from './GraphCanvas'
import { useGraph } from './useGraph'

/**
 * 关系类型的图例，与 graphLayout.edgeStyle 的视觉定义一一对应。
 * 显式标注类型，否则数组字面量会把 `type` 拓宽成 string，丢失字面量联合。
 */
const RELATION_LEGEND: { type: RelationType; stroke: string; dash?: string; hint: string }[] = [
  { type: 'contains', stroke: '#1b6b54', dash: undefined, hint: '父概念 → 子概念' },
  { type: 'prerequisite', stroke: '#2e5a7a', dash: '6 4', hint: '前置知识 → 后继知识' },
  { type: 'related', stroke: '#9c9288', dash: '2 4', hint: '无向，同源或同节' },
]

/**
 * 图例。**只列实际会出现的状态。**
 *
 * 曾经这里有一条橙色的「存疑」，而它标出了四成知识点 ——
 * 图例上的一格颜色，对应到图上是一大片，反而让"哪条真有问题"无从判断。
 * 列了却极少出现、或出现了也不可信的状态，不该占图例的位置。
 */
const STATUS_LEGEND = [
  { status: 'trusted', color: '#1b6b54' },
  { status: 'unverified', color: '#d5cbbb' },
]

export function KnowledgeGraphView() {
  const {
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
    selectedId,
    selectNode,
    point,
    checks,
    relations,
    pointLoading,
    rebuild,
    runVerify,
  } = useGraph()

  const selectedDocument = documents.find((d) => d.id === documentId) ?? null
  const running = verifyStatus?.state === 'queued' || verifyStatus?.state === 'running'
  //: 当前正在学的知识点 —— 画布据此高亮，实现"地图与 Tutor 联动"
  const { focus, startLearning } = useLearning()
  //: 侧栏是否收起。
  //:
  //: **知识地图是"工作台"，不是"阅读栏"。** 侧栏常驻 300px，
  //: 在 1262px 的屏上等于把画布砍掉三分之一 —— 而地图恰恰是最吃横向空间的地方
  //: （一个分带五个节点就要 894px 宽）。
  //: 默认展开（首次进来需要选资料），但允许一键把空间还给地图。
  const [asideOpen, setAsideOpen] = useState(true)

  return (
    <div className="flex min-h-0 flex-1 gap-4">
      {/* ------------------------------------------------------------ 左栏 */}
      {asideOpen ? (
      <aside className="flex min-h-0 w-[300px] shrink-0 flex-col gap-3 overflow-y-auto">
        <div className="flex items-center justify-between px-1">
          <span className="meta">设置</span>
          <button
            type="button"
            onClick={() => setAsideOpen(false)}
            title="收起侧栏，把空间让给地图"
            className="meta rounded-md px-1.5 py-0.5 transition-colors hover:bg-paper-sunken hover:text-ink-1"
          >
            收起 ›
          </button>
        </div>
        <section className="rounded-xl border border-line bg-paper-raised p-3">
          <p className="mb-2 text-xs font-medium text-ink-2">选择资料</p>
          <select
            value={documentId ?? ''}
            onChange={(event) => setDocumentId(Number(event.target.value))}
            className="w-full rounded-lg border border-line bg-paper-raised px-2 py-1.5 text-xs text-ink-2 outline-none focus:border-moss-line"
          >
            {documents.length === 0 && <option value="">（暂无资料）</option>}
            {documents.map((doc) => (
              <option key={doc.id} value={doc.id}>
                {doc.file_name} · {doc.kp_count} 个知识点
              </option>
            ))}
          </select>

          <div className="mt-3 flex gap-2">
            <button
              type="button"
              disabled={busy || documentId === null || (selectedDocument?.kp_count ?? 0) < 2}
              onClick={() => void rebuild()}
              className="flex-1 rounded-lg bg-moss px-2 py-1.5 text-xs font-medium text-white transition hover:bg-moss-ink disabled:cursor-not-allowed disabled:bg-line-strong"
            >
              重新梳理关系
            </button>
            <button
              type="button"
              disabled={busy || running || documentId === null}
              onClick={() => void runVerify()}
              className="flex-1 rounded-lg border border-line bg-paper-raised px-2 py-1.5 text-xs font-medium text-ink-2 transition hover:border-moss-line hover:text-moss disabled:cursor-not-allowed disabled:text-line-strong"
            >
              {running ? '正在核对…' : '核对可信度'}
            </button>
          </div>
        </section>

        {notice && (
          <div className="flex items-start justify-between gap-2 rounded-lg border border-moss-line bg-moss-soft px-3 py-2">
            <p className="text-xs leading-relaxed text-moss-ink">{notice}</p>
            <button
              type="button"
              onClick={() => setNotice(null)}
              className="shrink-0 text-xs text-moss hover:text-moss-ink"
            >
              ×
            </button>
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-brick-line bg-brick-soft px-3 py-2 text-xs text-brick-ink">
            {error}
          </div>
        )}

        {/* ---------------------------------------------------------- 图谱概览 */}
        <section className="rounded-xl border border-line bg-paper-raised p-3">
          <p className="mb-2 text-xs font-medium text-ink-2">这份资料</p>
          {loading && <p className="text-2xs text-ink-4">加载中…</p>}
          {graph && (
            <p className="text-xs leading-relaxed text-ink-2">
              我读出了 {graph.stats.node_count} 个知识点，找到 {graph.stats.edge_count} 条关系。
              {graph.stats.isolated_nodes > 0 && (
                <span className="text-sienna">
                  还有 {graph.stats.isolated_nodes} 个暂时没连上。
                </span>
              )}
              {Object.entries(graph.stats.by_type).length > 0 && (
                <span className="block mt-1.5 text-2xs text-ink-3">
                  {Object.entries(graph.stats.by_type)
                    .map(
                      ([type, count]) =>
                        `${RELATION_LABEL[type as keyof typeof RELATION_LABEL] ?? type} ${count}`,
                    )
                    .join(' · ')}
                </span>
              )}
            </p>
          )}
          {graph && graph.stats.edge_count === 0 && (
            <p className="mt-2 text-2xs leading-relaxed text-ink-4">
              还没梳理出关系。点「重新梳理关系」，我会依据章节结构和原文出处把知识点连起来。
            </p>
          )}
        </section>

        {/* ---------------------------------------------------------- 可信度核对 */}
        {verifyStatus && verifyStatus.checked_points > 0 && (
          <section className="rounded-xl border border-line bg-paper-raised p-3">
            <div className="mb-1 flex items-center justify-between">
              <p className="text-xs font-medium text-ink-2">可信度核对</p>
              <span className="text-2xs text-ink-4">
                {verifyStatus.checked_points}/{verifyStatus.total_points}
              </span>
            </div>
            {running && (
              <>
                <div className="h-1.5 w-full overflow-hidden rounded-full bg-paper-sunken">
                  <div
                    className="h-full rounded-full bg-moss transition-all"
                    style={{ width: `${verifyStatus.progress}%` }}
                  />
                </div>
                <p className="mt-1 text-2xs text-ink-4">{verifyStatus.detail}</p>
              </>
            )}
            <div className="mt-2 space-y-1">
              {Object.entries(verifyStatus.by_status).map(([status, count]) => (
                <div key={status} className="flex justify-between text-xs">
                  <span className="text-ink-3">
                    {VERIFY_STATUS_LABEL[status] ?? status}
                  </span>
                  <span className="tnum text-ink-2">{count}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* -------------------------------------------------------------- 图例 */}
        <section className="rounded-xl border border-line bg-paper-raised p-3">
          <p className="mb-2 text-xs font-medium text-ink-2">图例</p>
          <div className="space-y-1.5">
            {RELATION_LEGEND.map((item) => (
              <div key={item.type} className="flex items-center gap-2">
                <svg width="34" height="8" className="shrink-0">
                  <line
                    x1="0"
                    y1="4"
                    x2="34"
                    y2="4"
                    stroke={item.stroke}
                    strokeWidth={2}
                    strokeDasharray={item.dash}
                  />
                </svg>
                <span className="text-xs text-ink-2">
                  {RELATION_LABEL[item.type]}
                </span>
                <span className="text-2xs text-ink-4">{item.hint}</span>
              </div>
            ))}
          </div>
          <div className="mt-3 border-t border-paper-sunken pt-2">
            <p className="mb-1.5 text-2xs text-ink-4">左边框颜色 = 这条知识可不可信</p>
            <div className="flex flex-wrap gap-2">
              {STATUS_LEGEND.map((item) => (
                <span key={item.status} className="flex items-center gap-1">
                  <span
                    className="inline-block h-2.5 w-1.5 rounded-full"
                    style={{ background: item.color }}
                  />
                  <span className="text-2xs text-ink-3">
                    {VERIFY_STATUS_LABEL[item.status]}
                  </span>
                </span>
              ))}
            </div>
          </div>
        </section>

        {capabilities && !capabilities.tavily_configured && (
          <p className="rounded-lg border border-sienna-line bg-sienna-soft px-3 py-2 text-2xs leading-relaxed text-sienna-ink">
            现在还查不了网，所以我只能依据你的资料自己判断。
            配上联网的能力后，我可以再帮你上网核对一遍。
          </p>
        )}
      </aside>
      ) : (
        <button
          type="button"
          onClick={() => setAsideOpen(true)}
          title="展开侧栏（选择资料 / 重新梳理关系 / 核对可信度）"
          className="meta shrink-0 self-start rounded-md border border-line bg-paper-raised px-1.5 py-1 transition-colors hover:border-moss-line hover:text-moss"
        >
          ‹ 设置
        </button>
      )}

      {/* ------------------------------------------------------------ 画布 */}
      <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        {/*
          联动提示：正在学的知识点若属于另一份资料，当前画布上是找不到它的。
          与其让人以为"地图里没有"，不如直接给一个切过去的入口。
        */}
        {focus && focus.documentId !== null && focus.documentId !== documentId && (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-moss-line bg-moss-soft px-3 py-2">
            <p className="text-xs text-moss-ink">
              你正在学「{focus.title}」，它属于另一份资料。
            </p>
            <button
              type="button"
              onClick={() => setDocumentId(focus.documentId)}
              className="text-xs font-medium text-moss underline underline-offset-2"
            >
              切过去看看
            </button>
          </div>
        )}

        <div className="flex items-center justify-between">
          <h2 className="truncate text-sm font-medium text-ink-2">
            {graph ? graph.document_name : '知识图谱'}
          </h2>
          {graph && (
            <p className="shrink-0 text-xs text-ink-4">
              {graph.stats.node_count} 个知识点 · {graph.stats.edge_count} 条关系 ·
              点一下看详情和原文出处
            </p>
          )}
        </div>
        <div className="min-h-0 flex-1">
          {graph ? (
            <GraphCanvas
              nodes={graph.nodes}
              edges={graph.edges}
              selectedId={selectedId}
              onSelect={selectNode}
              learningFocusId={focus?.kpId ?? null}
            />
          ) : (
            <div className="flex h-full items-center justify-center rounded-xl border border-dashed border-line text-xs text-ink-4">
              {loading ? '正在加载图谱…' : '请选择一份资料'}
            </div>
          )}
        </div>
      </main>

      {/* ---------------------------------------------------------- 详情面板 */}
      {point && (
        <div className="flex w-[340px] shrink-0 flex-col gap-3">
          {/* 与 Tutor 联动的入口：从这个知识点回到学习 */}
          <button
            type="button"
            onClick={() =>
              void startLearning({
                kpId: point.id,
                title: point.title,
                documentId: point.document_id ?? null,
              })
            }
            className="btn-primary w-full text-sm"
          >
            带着我学这个知识点
          </button>
          <SourceViewer
            point={point}
            loading={pointLoading}
            error={null}
            onClose={() => selectNode(point.id)}
            checks={checks}
            relations={relations}
            onJumpTo={selectNode}
          />
        </div>
      )}
    </div>
  )
}
