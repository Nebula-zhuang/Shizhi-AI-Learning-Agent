import type { KnowledgePointSummary } from '../../api/library'

interface KnowledgePointCardProps {
  point: KnowledgePointSummary
  selected: boolean
  onSelect: (id: number) => void
}

/**
 * 知识点卡片。
 *
 * 来源页码是这张卡上最重要的信息 —— 它让「这个知识点出自哪一页」变得可核对。
 * 点击卡片会在右侧展开原文，这是本项目「可溯源」承诺的入口。
 *
 * ## 为什么这里**不再显示可信度标签**
 *
 * 卡片列表是学习者的主阅读流。每张卡右上角挂一个「可信 / 存疑 / 还没核对」
 * 有两个问题：
 *
 *   1. **绝大多数是"还没核对"** —— 没跑过校验的知识点占多数，
 *      一排灰标签除了制造噪音，不传递任何信息。
 *   2. **它把判断变成了一枚徽章。** 学习者看到「可信」就信了 ——
 *      但那是系统基于规则 / 模型 / 外部检索给出的**倾向**，
 *      不是一句可以照着信的结论。
 *
 * 该做的是让助教在合适的时候**用话解释清楚**
 * （"这点我查过，和你的资料一致" / "网上说法有出入，我按你的资料讲"），
 * 而不是贴个章。
 *
 * 所以可信度改为**纯后台留存**（`knowledge_checks` 表照旧记录），
 * 由助教在对话里自然带出 —— 见后端 `services/verify_voice.py`。
 *
 * 同样刻意**不显示**模型自报的"置信度 95%" —— 那是给开发者的数字。
 */
export function KnowledgePointCard({ point, selected, onSelect }: KnowledgePointCardProps) {
  const pages = point.source_pages ?? []

  return (
    <button
      type="button"
      onClick={() => onSelect(point.id)}
      className={[
        'w-full rounded-xl border px-4 py-3 text-left transition',
        selected
          ? 'border-moss-line bg-moss-soft/50 ring-1 ring-moss-line'
          : 'border-line bg-paper-raised hover:border-line-strong',
      ].join(' ')}
    >
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-medium text-ink-1">{point.title}</h3>
      </div>

      {point.summary && (
        <p className="mt-1 text-xs leading-relaxed text-ink-3">{point.summary}</p>
      )}

      {pages.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1">
          {pages.slice(0, 6).map((page) => (
            <span
              key={page}
              className="rounded bg-paper-sunken px-1.5 py-0.5 text-2xs text-ink-3"
            >
              第 {page} 页
            </span>
          ))}
          {pages.length > 6 && (
            <span className="text-2xs text-ink-4">+{pages.length - 6}</span>
          )}
        </div>
      )}
    </button>
  )
}
