import { DifficultyBadge } from '../../components/DifficultyBadge'
import { MarkdownView } from '../../components/MarkdownView'
import {
  CHECK_TYPE_LABEL,
  CHECK_VERDICT_LABEL,
  RELATION_LABEL,
  RELATION_SOURCE_LABEL,
  VERIFY_STATUS_LABEL,
  type KnowledgeCheckResponse,
  type PointRelationItem,
} from '../../api/graph'
import type { KnowledgePointDetail } from '../../api/library'

interface SourceViewerProps {
  point: KnowledgePointDetail
  loading: boolean
  error: string | null
  onClose: () => void
  /** P2 可选：该知识点的校验收据（规则 / 模型 / 联网 三层分开） */
  checks?: KnowledgeCheckResponse | null
  /** P2 可选：该知识点参与的关系 */
  relations?: PointRelationItem[] | null
  /** P2 可选：点击关系对端时跳转过去 */
  onJumpTo?: (kpId: number) => void
}

/** 校验结论对应的配色。让"通过"与"存疑"在同一屏里一眼可分。 */
const VERDICT_STYLE: Record<string, string> = {
  passed: 'bg-moss-soft text-moss-ink ring-moss-line',
  // `suspicious`（存疑）已删除，并入 unsupported。
  // 配色也跟着从砖红降到赭 —— "没找到依据"不是错误，不该用警报色。
  unsupported: 'bg-sienna-soft text-sienna-ink ring-sienna-line',
  skipped: 'bg-paper-sunken text-ink-3 ring-line',
  error: 'bg-brick-soft text-brick-ink ring-brick-line',
}

/**
 * 知识点详情 + 来源原文 + （P2）关系与校验收据。
 *
 * 上半部分是知识点本身的讲解，下半部分是它在原文中的出处。
 * 把两者放在同一屏是刻意的：学生可以立刻核对「AI 讲的」与「书上写的」是否一致 ——
 * 这正是本项目和普通 AI 问答最本质的区别。
 *
 * P2 追加的「关系」与「校验收据」都是可选区块：不传 props 时组件行为与 P1 完全一致，
 * 因此 P1 资料库里的调用点一行都不用改。
 */
export function SourceViewer({
  point,
  loading,
  error,
  onClose,
  checks,
  relations,
  onJumpTo,
}: SourceViewerProps) {
  return (
    <aside className="flex h-full min-h-0 flex-col rounded-xl border border-line bg-paper-raised">
      <header className="flex items-start justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium text-ink-1">{point.title}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <DifficultyBadge difficulty={point.difficulty} importance={point.importance} />
            <span className="rounded-full bg-paper-sunken px-2 py-0.5 text-2xs text-ink-2 ring-1 ring-line">
              {VERIFY_STATUS_LABEL[point.verify_status] ?? point.verify_status}
            </span>
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 rounded-md px-2 py-1 text-xs text-ink-4 transition hover:bg-paper-sunken hover:text-ink-2"
        >
          收起
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {error && (
          <div className="mb-3 rounded-lg border border-brick-line bg-brick-soft px-3 py-2 text-xs text-brick-ink">
            {error}
          </div>
        )}
        {loading && <p className="text-xs text-ink-4">加载中…</p>}

        <MarkdownView>{point.details || point.summary || '_（无展开讲解）_'}</MarkdownView>

        {point.key_points && point.key_points.length > 0 && (
          <div className="mt-4 rounded-lg bg-paper-sunken px-3 py-2">
            <p className="mb-1 text-xs font-medium text-ink-2">要点</p>
            <ul className="list-disc space-y-0.5 pl-4">
              {point.key_points.map((kp, index) => (
                <li key={index} className="text-xs text-ink-2">
                  {kp}
                </li>
              ))}
            </ul>
          </div>
        )}

        {point.tags && point.tags.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1">
            {point.tags.map((tag) => (
              <span
                key={tag}
                className="rounded bg-moss-soft px-2 py-0.5 text-2xs text-moss"
              >
                {tag}
              </span>
            ))}
          </div>
        )}

        {/* ---------------------------------------------------------- 溯源区 */}
        <div className="mt-5 border-t border-line pt-4">
          <p className="mb-2 text-xs font-medium text-ink-2">
            来源原文（共 {point.sources.length} 处）
          </p>
          {point.sources.length === 0 && (
            <p className="text-xs text-ink-4">未找到对应的原文块。</p>
          )}
          <div className="space-y-2">
            {point.sources.map((source) => (
              <article
                key={source.chunk_index}
                className="rounded-lg border border-line bg-paper-sunken/60 px-3 py-2"
              >
                <div className="mb-1 flex items-center gap-2">
                  <span className="rounded bg-paper-raised px-1.5 py-0.5 text-2xs text-ink-3 ring-1 ring-line">
                    第 {source.page_start}
                    {source.page_end !== source.page_start ? `-${source.page_end}` : ''} 页
                  </span>
                  <span className="text-2xs text-ink-4">
                    块 #{source.chunk_index} · {source.block_type}
                  </span>
                </div>
                <p className="whitespace-pre-wrap text-xs leading-relaxed text-ink-2">
                  {source.content}
                </p>
              </article>
            ))}
          </div>
        </div>

        {/* ------------------------------------------------- P2：知识点关系 */}
        {relations && relations.length > 0 && (
          <div className="mt-5 border-t border-line pt-4">
            <p className="mb-2 text-xs font-medium text-ink-2">
              相关知识（{relations.length}）
            </p>
            <div className="space-y-1.5">
              {relations.map((relation) => (
                <button
                  key={relation.id}
                  type="button"
                  onClick={() => onJumpTo?.(relation.peer_id)}
                  className="block w-full rounded-lg border border-line bg-paper-raised px-2.5 py-2 text-left transition hover:border-moss-line hover:bg-moss-soft/40"
                >
                  <div className="flex items-center gap-1.5">
                    <span className="rounded bg-moss-soft px-1.5 py-0.5 text-2xs font-medium text-moss">
                      {RELATION_LABEL[relation.relation_type] ?? relation.relation_type}
                    </span>
                    <span className="truncate text-xs text-ink-2">
                      {relation.direction === 'out' ? '→' : '←'} {relation.peer_title}
                    </span>
                  </div>
                  <p className="mt-1 text-2xs leading-relaxed text-ink-4">
                    依据：{RELATION_SOURCE_LABEL[relation.source] ?? relation.source} ·{' '}
                    {relation.evidence}
                  </p>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* ----------------------------------------------- P2：校验收据 */}
        {checks && checks.total > 0 && (
          <div className="mt-5 border-t border-line pt-4">
            <p className="mb-1 text-xs font-medium text-ink-2">
              这条知识可不可信（查了 {checks.total} 项）
            </p>
            <p className="mb-2 text-2xs leading-relaxed text-ink-4">
              我用了三种办法分开查：<span className="text-ink-2">比对原文</span>
              是看资料里到底写没写、<span className="text-ink-2">独立再判断</span>
              是我自己重新想一遍、<span className="text-ink-2">上网查证</span>
              是找外部资料对照。三种办法的把握不一样，所以不合成一个结论。
            </p>
            <div className="space-y-2">
              {checks.groups
                .filter((group) => group.latest !== null)
                .map((group) => {
                  const latest = group.latest!
                  return (
                    <div
                      key={group.check_type}
                      className="rounded-lg border border-line bg-paper-sunken/60 px-2.5 py-2"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-xs font-medium text-ink-2">
                          {CHECK_TYPE_LABEL[group.check_type] ?? group.check_type}
                        </span>
                        <span
                          className={`rounded-full px-1.5 py-0.5 text-2xs ring-1 ${
                            VERDICT_STYLE[latest.verdict] ?? VERDICT_STYLE.skipped
                          }`}
                        >
                          {CHECK_VERDICT_LABEL[latest.verdict] ?? latest.verdict}
                        </span>
                      </div>
                      {latest.reason && (
                        <p className="mt-1 text-2xs leading-relaxed text-ink-3">
                          {latest.reason}
                        </p>
                      )}
                      <p className="mt-1 text-2xs text-ink-4">
                        由 {latest.engine || '未知'} 判定
                        {group.count > 1 ? ` · 历史 ${group.count} 次` : ''}
                      </p>
                      {latest.source_urls && latest.source_urls.length > 0 && (
                        <div className="mt-1 space-y-0.5">
                          {latest.source_urls.slice(0, 3).map((url) => (
                            <a
                              key={url}
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              className="block truncate text-2xs text-slate-blue hover:underline"
                            >
                              {url}
                            </a>
                          ))}
                        </div>
                      )}
                    </div>
                  )
                })}
            </div>
          </div>
        )}
      </div>
    </aside>
  )
}
