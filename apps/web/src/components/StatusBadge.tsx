import type { ParseStatus } from '../api/library'

const LABELS: Record<ParseStatus, string> = {
  pending: '排队中',
  parsing: '读取中',
  chunking: '整理中',
  extracting: '提炼中',
  ready: '已完成',
  failed: '失败',
}

const STYLES: Record<ParseStatus, string> = {
  pending: 'bg-paper-sunken text-ink-2 ring-line',
  parsing: 'bg-slate-blue-soft text-slate-blue ring-slate-blue-line',
  chunking: 'bg-slate-blue-soft text-slate-blue ring-slate-blue-line',
  extracting: 'bg-moss-soft text-moss-ink ring-moss-line',
  ready: 'bg-moss-soft text-moss-ink ring-moss-line',
  failed: 'bg-brick-soft text-brick-ink ring-brick-line',
}

/** 处理状态徽标。进行中的状态带一个脉冲圆点，让"还在跑"这件事一眼可见。 */
export function StatusBadge({ status }: { status: ParseStatus }) {
  const running = status !== 'ready' && status !== 'failed'
  return (
    // shrink-0 + whitespace-nowrap：徽章**永远不该被挤成两行**。
    // 它常和可伸缩的文件名同处一行，缺这两个属性时"已完成"会被折成"已完/成"（实测踩到）。
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-sm px-2 py-0.5 text-2xs ring-1 ${STYLES[status]}`}
    >
      <span
        className={[
          'h-1.5 w-1.5 rounded-full',
          running ? 'animate-pulse bg-current' : 'bg-current',
        ].join(' ')}
      />
      {LABELS[status]}
    </span>
  )
}
