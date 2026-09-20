/**
 * 图谱布局计算（纯函数，无 React 依赖）。
 *
 * **刻意不引入 d3 或任何图布局库。**
 *
 * 理由：P2 的关系以 `contains`（章节层级）为主，这类关系本身就是分层的，
 * 用「按章节深度分层、同层按原文顺序横排」的确定性布局语义最贴切。
 * 力导向布局会带来三个问题：每次渲染位置都在变（用户找不到刚才那个知识点）、
 * 需要额外依赖、结果不可测试。分层布局没有这些问题，而且更好读 ——
 * 学生看到的图与教材的目录结构是同构的。
 *
 * 布局是纯函数，同样的输入永远得到同样的坐标，因此可以被测试覆盖。
 */

import type { GraphEdge, GraphNode } from '../../api/graph'

export const NODE_WIDTH = 158
export const NODE_HEIGHT = 50
/** 同层节点之间的水平间距 */
const GAP_X = 26
/** 带内换行的行间距 */
const ROW_GAP = 66
/** 带与带之间的间距 */
const BAND_GAP = 54
/** 画布内边距 */
const PADDING = 40

/**
 * 单行最多放几个节点。
 *
 * 这个上限是为真实数据加的：一份 44 页的课程资料可以有 100+ 个知识点，
 * 而章节深度可能只有 3 层。如果只按深度分层，同一层会被摊成 40 多列的超宽行，
 * 用户必须横向拖很久才能看完 —— 图谱失去了"一眼看清结构"的意义。
 * 限制每行数量后，超长的层会自然折行，画布宽度被约束在可读范围内。
 *
 * ⚠️ 这是**上限**而不是实际值。实际每行几个由画布宽度决定（见 `layoutGraph`），
 * 因为"图多宽"必须跟着"画布多宽"走，否则会被缩放压小。
 */
const MAX_PER_ROW = 5

/** 宽度未知时的兜底每行数量 */
const DEFAULT_PER_ROW = 4

export interface LayoutOptions {
  /**
   * 画布可用宽度（像素）。
   *
   * 传了它，布局就会把每行节点数**算成刚好铺满这个宽度**，
   * 让后续的适配缩放落在 1 倍附近 —— 字才是正常大小。
   * 不传则用 `DEFAULT_PER_ROW`（离线测试、无画布场景）。
   */
  maxWidth?: number
}

export interface LaidOutNode extends GraphNode {
  x: number
  y: number
  width: number
  height: number
  /** 所属章节带的序号（从 0 开始） */
  band: number
  /** 在带内的行号，从 0 开始 */
  row: number
}

/** 一个章节带：同一小节下的知识点排成一条横向区域。 */
export interface LayoutBand {
  index: number
  label: string
  headingPath: string[]
  y: number
  height: number
  nodeCount: number
}

export interface GraphLayout {
  nodes: LaidOutNode[]
  bands: LayoutBand[]
  width: number
  height: number
}

/** 没有章节信息的节点归到一个兜底带里 */
const UNLABELED = '（未标注章节）'

/**
 * 图谱布局：**按章节分带**，带内按原文顺序横排、超出则折行。
 *
 * 为什么不用「按 depth 直接分层」：
 * 真实资料往往是「一个大章节下并列十几个小节」，depth 全是 1 或 2，
 * 按 depth 分层会把上百个节点摊成两三行超宽的长条。
 * 按小节分带则天然把节点切成 3-5 个一组，既贴合教材的目录结构，
 * 又让画布宽度有上界。
 *
 * 为什么不用力导向布局：位置每次都在变（用户找不到刚才看的节点）、
 * 需要额外依赖、结果不可测试。分层分带是确定性的，可以被单元测试覆盖。
 */
export function layoutGraph(nodes: GraphNode[], options: LayoutOptions = {}): GraphLayout {
  if (nodes.length === 0) {
    return { nodes: [], bands: [], width: PADDING * 2, height: PADDING * 2 }
  }

  // 每行放几个：**按可用宽度算，不用固定值**。
  //
  // 固定 5 个的后果（实测）：一行 5 个节点时图宽 958px，
  // 而画布在 1262px 的屏上只有 646px —— 于是整张图被缩到 **0.61 倍**，
  // 11.5px 的节点标题变成 7px，糊得看不清。
  // **用户说的"太窄"就是这个。**
  //
  // 按宽度算之后，布局的自然尺寸≈画布尺寸，缩放回到 1 倍附近，
  // 字就是正常大小；多出来的行靠上下拖拽看 ——
  // 这比"全都塞进去但看不清"有用得多。
  const perRowLimit = options.maxWidth
    ? Math.max(
        1,
        Math.min(
          MAX_PER_ROW,
          Math.floor((options.maxWidth - PADDING * 2 + GAP_X) / (NODE_WIDTH + GAP_X)),
        ),
      )
    : DEFAULT_PER_ROW

  // ------------------------------------------------ 1. 按 heading_path 分带
  interface Bucket {
    label: string
    headingPath: string[]
    members: GraphNode[]
    firstOrder: number
  }
  const buckets = new Map<string, Bucket>()

  for (const node of nodes) {
    const path = node.heading_path?.filter((part) => part && part.trim()) ?? []
    const key = path.length > 0 ? path.join(' / ') : UNLABELED
    const existing = buckets.get(key)
    if (existing) {
      existing.members.push(node)
      existing.firstOrder = Math.min(existing.firstOrder, node.order_index)
    } else {
      buckets.set(key, {
        label: path.length > 0 ? path[path.length - 1] : UNLABELED,
        headingPath: path,
        members: [node],
        firstOrder: node.order_index,
      })
    }
  }

  // 带本身按文档顺序排列
  const ordered = [...buckets.values()].sort(
    (a, b) => a.firstOrder - b.firstOrder || a.label.localeCompare(b.label),
  )

  // -------------------------------------------------------- 2. 逐带排布
  const laidOut: LaidOutNode[] = []
  const bands: LayoutBand[] = []

  // 先算出最宽的一行，用于横向居中
  let widest = 0
  for (const bucket of ordered) {
    const perRow = Math.min(bucket.members.length, perRowLimit)
    widest = Math.max(widest, perRow * NODE_WIDTH + (perRow - 1) * GAP_X)
  }

  let cursorY = PADDING

  ordered.forEach((bucket, bandIndex) => {
    const members = bucket.members
      .slice()
      .sort((a, b) => a.order_index - b.order_index || a.id - b.id)

    const bandTop = cursorY
    let row = 0

    for (let start = 0; start < members.length; start += perRowLimit) {
      const rowMembers = members.slice(start, start + perRowLimit)
      const rowWidth = rowMembers.length * NODE_WIDTH + (rowMembers.length - 1) * GAP_X
      const startX = PADDING + (widest - rowWidth) / 2
      const y = bandTop + row * ROW_GAP

      rowMembers.forEach((node, column) => {
        laidOut.push({
          ...node,
          x: startX + column * (NODE_WIDTH + GAP_X),
          y,
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          band: bandIndex,
          row,
        })
      })
      row += 1
    }

    const bandHeight = (row - 1) * ROW_GAP + NODE_HEIGHT
    bands.push({
      index: bandIndex,
      label: bucket.label,
      headingPath: bucket.headingPath,
      y: bandTop,
      height: bandHeight,
      nodeCount: members.length,
    })
    cursorY = bandTop + bandHeight + BAND_GAP
  })

  return {
    nodes: laidOut,
    bands,
    width: widest + PADDING * 2,
    height: cursorY - BAND_GAP + PADDING,
  }
}

/** 边的走向：同一行内横向连；跨行则竖直连。都用二次贝塞尔画成柔和曲线。 */
export interface EdgePath {
  d: string
  labelX: number
  labelY: number
}

export function edgePath(from: LaidOutNode, to: LaidOutNode): EdgePath {
  const sameRow = from.band === to.band && from.row === to.row

  // 同一行：从节点侧面出发，避免线穿过节点矩形
  if (sameRow) {
    const fromX = from.x + from.width / 2
    const toX = to.x + to.width / 2
    const midY = from.y + from.height / 2
    const leftToRight = toX >= fromX
    const startX = leftToRight ? from.x + from.width : from.x
    const endX = leftToRight ? to.x : to.x + to.width
    const curve = Math.max(Math.abs(endX - startX) * 0.35, 18)
    return {
      d: `M ${startX} ${midY} C ${startX + curve * (leftToRight ? 1 : -1)} ${midY - 30}, ${
        endX - curve * (leftToRight ? 1 : -1)
      } ${midY - 30}, ${endX} ${midY}`,
      labelX: (startX + endX) / 2,
      labelY: midY - 20,
    }
  }

  // 跨行：竖直方向的柔和曲线
  const fromX = from.x + from.width / 2
  const fromY = from.y + from.height
  const toX = to.x + to.width / 2
  const downward = to.y >= from.y
  const startY = downward ? fromY : from.y
  const endY = downward ? to.y : to.y + to.height
  const controlOffset = Math.max(Math.abs(endY - startY) * 0.45, 24)

  return {
    d: `M ${fromX} ${startY} C ${fromX} ${startY + controlOffset}, ${toX} ${
      endY - controlOffset
    }, ${toX} ${endY}`,
    labelX: (fromX + toX) / 2,
    labelY: (startY + endY) / 2,
  }
}

/**
 * 依据校验状态取节点描边色。
 *
 * 现在实际只有两种取值：**核对过的（绿）与还没核对过的（灰）**。
 *
 * 原来的暖色系（存疑 / 有出入 / 可能过时）都是"这条可能有问题"的暗示，
 * 而它们加起来标出了 40% 的知识点 —— 等于把整张图染成一片警告色，
 * 反而让人分不清哪条真有问题。**少一种颜色，信号反而更清楚。**
 *
 * `conflict` / `outdated` 两支是保留位，当前不产出，留着以免历史数据变成裸色。
 */
export function statusStroke(status: string): string {
  switch (status) {
    case 'trusted':
      return 'var(--color-moss)' // moss
    case 'conflict':
      return 'var(--color-brick)' // brick —— 保留位
    case 'outdated':
      return 'var(--color-sienna-ink)' // sienna-ink —— 保留位
    default:
      return 'var(--color-line-strong)' // line-strong
  }
}

/** 依据关系类型取线的样式。三种关系必须视觉可分，否则"能看到关系类型"就是空话。 */
export function edgeStyle(type: string): { stroke: string; dash?: string; width: number } {
  switch (type) {
    case 'contains':
      return { stroke: 'var(--color-moss)', width: 1.8 } // moss，实线
    case 'prerequisite':
      return { stroke: 'var(--color-slate-blue)', width: 1.6, dash: '6 4' } // slate-blue，虚线
    case 'related':
      return { stroke: 'var(--color-ink-4)', width: 1.2, dash: '2 4' } // ink-4，点线
    default:
      return { stroke: 'var(--color-line-strong)', width: 1 }
  }
}

/** 有向关系才画箭头头。related 是无向的，画箭头会误导。 */
export function isDirected(type: string): boolean {
  return type === 'contains' || type === 'prerequisite'
}

/** 某条边是否与当前选中节点相连 —— 用于高亮邻居、弱化无关部分。 */
export function touchesNode(edge: GraphEdge, nodeId: number | null): boolean {
  if (nodeId === null) return false
  return edge.from_kp_id === nodeId || edge.to_kp_id === nodeId
}
