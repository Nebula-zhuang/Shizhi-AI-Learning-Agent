/**
 * 知识图谱画布（手写 SVG）。
 *
 * 只负责「画」，不负责取数与详情 —— 布局由 graphLayout.ts 的纯函数给出，
 * 因此同样的数据永远画出同样的图，用户不会因为刷新而找不到刚才看的节点。
 *
 * ## 为什么不用 React Flow
 *
 * 这一页要的动效（节点逐个浮现、连线在聚焦时流动、当前学习点持续呼吸、
 * 悬停时邻居点亮而其余退到背景）都要直接控制 SVG 元素。
 * 通用图库给的是"节点组件 + 边组件"的抽象，做这些反而要跟它对抗；
 * 而布局引擎是纯函数且有测试覆盖，自研在这里是更省力的路。
 *
 * ## 交互
 *
 *   · 滚轮缩放，**以指针为锚点**（以中心为锚点的话，想放大的那块总会滑走）
 *   · 拖拽平移；点在节点上不平移 —— 那是"打开详情"的手势
 *   · 悬停/点选：邻居点亮，无关节点退到 0.28。两百个节点的图必须有办法聚焦
 *   · 键盘：Tab 逐个选中、回车打开详情
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { motion, useReducedMotion } from 'motion/react'

import {
  RELATION_LABEL,
  RELATION_SOURCE_LABEL,
  type GraphEdge,
  type GraphNode,
} from '../../api/graph'
import {
  edgePath,
  edgeStyle,
  isDirected,
  layoutGraph,
  statusStroke,
  touchesNode,
  type LaidOutNode,
} from './graphLayout'

interface GraphCanvasProps {
  nodes: GraphNode[]
  edges: GraphEdge[]
  selectedId: number | null
  onSelect: (id: number) => void
  /**
   * 当前正在学的知识点 id（来自共享的 LearningContext）。
   * 有这个 id 时对应节点带一圈持续呼吸的高亮环 —— "地图上哪个是我在学的"一眼可见。
   * 与 `selectedId`（悬停/点选的强调）是两个概念，互不影响。
   */
  learningFocusId?: number | null
}

/** SVG 里没法自动换行，标题过长时手工截断 */
function ellipsize(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`
}

const MIN_SCALE = 0.4
const MAX_SCALE = 2.2

/**
 * 载入时自动适配的放大上限。
 *
 * 只放不缩的图会显得小，但也不能把两个节点的图撑成一屏大。
 * 1.35 是"看得出来变大了、又不至于荒谬"的位置。
 */
const FIT_MAX_SCALE = 1.35

/** 适配时四周留的空白（像素） */
const FIT_PADDING = 26

/**
 * 纵向放不下时，缩到这个比例就别再缩了 —— 剩下的交给上下拖拽。
 *
 * **为什么要有这个下限**：如果一门心思"把整张图塞进容器"，
 * 一个十几行的知识地图会被压到 0.1 倍，字全糊成一团，
 * 那比"看不全"更没用。宽度铺满 + 纵向可拖，才是能读的。
 */
const FIT_MIN_SCALE = 0.55

export function GraphCanvas({
  nodes,
  edges,
  selectedId,
  onSelect,
  learningFocusId,
}: GraphCanvasProps) {
  const [hoveredId, setHoveredId] = useState<number | null>(null)
  const still = useReducedMotion()

  // ── 视口
  const [view, setView] = useState({ k: 1, x: 0, y: 0 })
  const dragging = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null)
  const viewport = useRef<HTMLDivElement>(null)
  //: 容器尺寸。**必须实测而不能猜** —— 侧栏收放、窗口缩放都会改变它，
  //: 而"图多宽"完全取决于它。
  const [box, setBox] = useState<{ w: number; h: number }>({ w: 0, h: 0 })
  //: 最近一次适配后的视口。用来判断"现在是不是已经处于适配状态" ——
  //: 用户拖过或缩过之后才显示"回到适配"。
  const fitted = useRef({ k: 1, x: 0, y: 0 })

  const layout = useMemo(
    // 把画布宽度交给布局 —— 每行几个节点由它决定，而不是写死。
    // 写死会导致"图比画布宽 → 被缩放压小 → 字糊"（实测压到过 0.61 倍）。
    // 首帧还没量到宽度时传 undefined，走默认值；量到之后会自动重排。
    () => layoutGraph(nodes, { maxWidth: box.w || undefined }),
    [nodes, box.w],
  )
  const positioned = useMemo(() => {
    const map = new Map<number, LaidOutNode>()
    for (const node of layout.nodes) map.set(node.id, node)
    return map
  }, [layout])

  // 跟踪容器尺寸
  useEffect(() => {
    const element = viewport.current
    if (!element) return
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect
      if (rect) setBox({ w: rect.width, h: rect.height })
    })
    observer.observe(element)
    const rect = element.getBoundingClientRect()
    setBox({ w: rect.width, h: rect.height })
    return () => observer.disconnect()
  }, [])

  /**
   * 把整张图适配进容器，并居中。
   *
   * ## 之前为什么"太窄"
   *
   * 原实现是 `scale(1)` 直接贴上去的 —— **从不计算适配**。后果有两个方向：
   *
   *   · 图比容器小 → 缩在左上角，一圈空白，看起来又小又窄
   *   · 图比容器大 → 直接溢出，必须拖拽才能看全
   *
   * 一个分带 5 个节点就是 `5×158 + 4×26 = 894px`，早就超了。
   *
   * ## 适配策略：横铺满，纵可拖
   *
   * 取宽度比作为基准（图要占满横向空间），只有在**纵向也放得下**时才允许缩得更小；
   * 纵向放不下就不缩了 —— 底线是 `FIT_MIN_SCALE`，剩下的交给拖拽。
   * 把一屏十几行的地图压成 0.1 倍字全糊掉，比"看不全"更没用。
   */
  const fitView = useCallback(() => {
    const element = viewport.current
    if (!element) return
    const rect = element.getBoundingClientRect()
    if (!rect.width || !rect.height || layout.width <= 0 || layout.height <= 0) return

    const availW = Math.max(rect.width - FIT_PADDING * 2, 1)
    const availH = Math.max(rect.height - FIT_PADDING * 2, 1)

    const byWidth = availW / layout.width
    const byHeight = availH / layout.height

    let k = byWidth
    // 纵向能完整放下就一起考虑；放不下就别为了"塞下"把字压糊
    if (byHeight >= FIT_MIN_SCALE) k = Math.min(k, byHeight)
    k = Math.max(FIT_MIN_SCALE, Math.min(k, FIT_MAX_SCALE))

    const drawnW = layout.width * k
    const drawnH = layout.height * k

    const next = {
      k,
      // 装得下就居中；装不下就贴左上角加留白，让人从开头看起
      x: drawnW <= rect.width ? (rect.width - drawnW) / 2 : FIT_PADDING,
      y: drawnH <= rect.height ? (rect.height - drawnH) / 2 : FIT_PADDING,
    }
    fitted.current = next
    setView(next)
  }, [layout.width, layout.height])

  // 换数据 / 容器尺寸变了就重新适配。
  // 依赖 box 是必要的：侧栏一收，横向空间变了，图得跟着重新铺满。
  useEffect(() => {
    fitView()
  }, [fitView, box.w, box.h])

  /** 当前视口是否就是适配后的状态（用户没拖过也没缩过） */
  const isFitted =
    Math.abs(view.k - fitted.current.k) < 0.01 &&
    Math.abs(view.x - fitted.current.x) < 2 &&
    Math.abs(view.y - fitted.current.y) < 2

  const emphasisId = selectedId ?? hoveredId
  const connectedIds = useMemo(() => {
    if (emphasisId === null) return new Set<number>()
    const set = new Set<number>([emphasisId])
    for (const edge of edges) {
      if (edge.from_kp_id === emphasisId) set.add(edge.to_kp_id)
      if (edge.to_kp_id === emphasisId) set.add(edge.from_kp_id)
    }
    return set
  }, [edges, emphasisId])

  const onWheel = useCallback((event: React.WheelEvent) => {
    event.preventDefault()
    const rect = viewport.current?.getBoundingClientRect()
    if (!rect) return
    const px = event.clientX - rect.left
    const py = event.clientY - rect.top

    setView((current) => {
      const next = Math.min(
        MAX_SCALE,
        Math.max(MIN_SCALE, current.k * (event.deltaY < 0 ? 1.12 : 0.89)),
      )
      const ratio = next / current.k
      return { k: next, x: px - (px - current.x) * ratio, y: py - (py - current.y) * ratio }
    })
  }, [])

  const onPointerDown = useCallback(
    (event: React.PointerEvent) => {
      if ((event.target as Element).closest('[data-kp-id]')) return
      dragging.current = { x: event.clientX, y: event.clientY, vx: view.x, vy: view.y }
      ;(event.currentTarget as Element).setPointerCapture(event.pointerId)
    },
    [view.x, view.y],
  )

  const onPointerMove = useCallback((event: React.PointerEvent) => {
    const start = dragging.current
    if (!start) return
    setView((current) => ({
      k: current.k,
      x: start.vx + (event.clientX - start.x),
      y: start.vy + (event.clientY - start.y),
    }))
  }, [])

  const endDrag = useCallback(() => {
    dragging.current = null
  }, [])

  if (nodes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-xs text-ink-4">
        这份资料还没有知识点，无法生成图谱。
      </div>
    )
  }

  return (
    <div
      ref={viewport}
      onWheel={onWheel}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerLeave={endDrag}
      className="relative h-full w-full cursor-grab overflow-hidden rounded-xl border border-line active:cursor-grabbing"
      style={{
        background:
          'radial-gradient(120% 90% at 50% 0%, var(--color-surface-2) 0%, var(--color-canvas) 78%)',
      }}
    >
      <svg
        width="100%"
        height="100%"
        className="block select-none"
        role="group"
        aria-label={`知识图谱，共 ${nodes.length} 个知识点`}
      >
        <defs>
          <marker
            id="arrow-contains"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-moss)" />
          </marker>
          <marker
            id="arrow-prerequisite"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-slate-blue)" />
          </marker>
        </defs>

        <g transform={`translate(${view.x}, ${view.y}) scale(${view.k})`}>
          {/* ───────────────────────────────────────── 章节带 */}
          {layout.bands.map((band, bandIndex) => (
            <motion.g
              key={`band-${band.index}`}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: 0.5, delay: bandIndex * 0.08 }}
            >
              <rect
                x={8}
                y={band.y - 26}
                width={Math.max(layout.width - 16, 40)}
                height={band.height + 40}
                rx={12}
                fill="var(--color-surface-1)"
                fillOpacity={0.55}
                stroke="var(--color-line)"
              />
              <text x={18} y={band.y - 10} fontSize={10.5} fill="var(--color-ink-3)" fontWeight={500}>
                {band.label}
              </text>
              <text
                x={Math.max(layout.width - 18, 20)}
                y={band.y - 10}
                fontSize={9.5}
                fill="var(--color-ink-4)"
                textAnchor="end"
              >
                {band.nodeCount} 个知识点
              </text>
            </motion.g>
          ))}

          {/* ───────────────────────────────────────── 关系边 */}
          <g>
            {edges.map((edge, index) => {
              const from = positioned.get(edge.from_kp_id)
              const to = positioned.get(edge.to_kp_id)
              if (!from || !to) return null

              const path = edgePath(from, to)
              const style = edgeStyle(edge.relation_type)
              const dimmed = emphasisId !== null && !touchesNode(edge, emphasisId)
              const directed = isDirected(edge.relation_type)

              return (
                <motion.g
                  key={`edge-${edge.id}`}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: dimmed ? 0.12 : 1 }}
                  transition={{ duration: 0.36, delay: Math.min(0.2 + index * 0.012, 0.9) }}
                >
                  {/* 聚焦时才出现的"流动层"：虚线缓慢位移，暗示关系有方向。
                      平时不画 —— 满屏都在流动会非常吵 */}
                  {emphasisId !== null && !dimmed && (
                    <path
                      d={path.d}
                      fill="none"
                      stroke={style.stroke}
                      strokeWidth={style.width + 1.4}
                      strokeOpacity={0.28}
                      strokeLinecap="round"
                      strokeDasharray="3 9"
                      style={still ? undefined : { animation: 'flow 2.2s linear infinite' }}
                    />
                  )}
                  <path
                    d={path.d}
                    fill="none"
                    stroke={style.stroke}
                    strokeWidth={style.width}
                    strokeDasharray={style.dash}
                    markerEnd={
                      directed
                        ? `url(#arrow-${edge.relation_type === 'contains' ? 'contains' : 'prerequisite'})`
                        : undefined
                    }
                  />
                  <g transform={`translate(${path.labelX}, ${path.labelY})`}>
                    <rect
                      x={-20}
                      y={-9}
                      width={40}
                      height={17}
                      rx={8}
                      fill="var(--color-surface-1)"
                      stroke={style.stroke}
                      strokeOpacity={0.45}
                    />
                    <text x={0} y={3} textAnchor="middle" fontSize={10} fill={style.stroke}>
                      {RELATION_LABEL[edge.relation_type] ?? edge.relation_type}
                    </text>
                  </g>
                  <title>
                    {`${from.title} → ${to.title}\n关系：${
                      RELATION_LABEL[edge.relation_type] ?? edge.relation_type
                    }\n依据：${RELATION_SOURCE_LABEL[edge.source] ?? edge.source}`}
                  </title>
                </motion.g>
              )
            })}
          </g>

          {/* ───────────────────────────────────────── 节点 */}
          <g>
            {layout.nodes.map((node, index) => {
              const selected = node.id === selectedId
              const dimmed = emphasisId !== null && !connectedIds.has(node.id)
              const stroke = statusStroke(node.verify_status)
              const learningHere = learningFocusId === node.id

              return (
                <motion.g
                  key={`node-${node.id}`}
                  initial={{ opacity: 0, scale: 0.88 }}
                  animate={{ opacity: dimmed ? 0.28 : 1, scale: 1 }}
                  transition={{
                    duration: 0.42,
                    delay: Math.min(0.15 + index * 0.022, 1.1),
                    ease: [0.16, 1, 0.3, 1],
                  }}
                  transform={`translate(${node.x}, ${node.y})`}
                  style={{ cursor: 'pointer' }}
                  onClick={() => onSelect(node.id)}
                  onMouseEnter={() => setHoveredId(node.id)}
                  onMouseLeave={() => setHoveredId((c) => (c === node.id ? null : c))}
                  role="button"
                  tabIndex={0}
                  aria-label={`${node.title}，难度 ${node.difficulty}，重要度 ${node.importance}`}
                  aria-pressed={selected}
                  data-kp-id={node.id}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      onSelect(node.id)
                    }
                  }}
                  className="outline-none focus-visible:opacity-100"
                >
                  {/* 正在学习：一圈持续呼吸的高亮环 */}
                  {learningHere && (
                    <motion.rect
                      x={-6}
                      y={-6}
                      width={node.width + 12}
                      height={node.height + 12}
                      rx={15}
                      fill="none"
                      stroke="var(--color-moss)"
                      strokeWidth={1.6}
                      strokeDasharray="4 4"
                      animate={still ? undefined : { opacity: [0.5, 1, 0.5] }}
                      transition={{ duration: 2.6, repeat: Infinity, ease: 'easeInOut' }}
                    />
                  )}

                  {/* 选中/悬停：一圈很轻的外发光 */}
                  {(selected || node.id === hoveredId) && (
                    <motion.rect
                      x={-4}
                      y={-4}
                      width={node.width + 8}
                      height={node.height + 8}
                      rx={14}
                      fill="none"
                      stroke="var(--color-moss)"
                      strokeWidth={0.9}
                      strokeOpacity={0.55}
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                    />
                  )}

                  <rect
                    width={node.width}
                    height={node.height}
                    rx={12}
                    fill="var(--color-surface-1)"
                    stroke={selected ? 'var(--color-moss)' : stroke}
                    strokeWidth={selected ? 2 : 1.2}
                  />
                  {/* 左缘色条 = 校验状态，替掉第一版那个硬编码的白底卡片 */}
                  <rect width={3.5} height={node.height} rx={1.75} fill={stroke} />
                  <text x={14} y={21} fontSize={11.5} fill="var(--color-ink-1)" fontWeight={500}>
                    {ellipsize(node.title, 13)}
                  </text>
                  <text x={14} y={37} fontSize={9.5} fill="var(--color-ink-4)">
                    难度 {node.difficulty} · 重要 {node.importance}
                    {node.source_pages?.length ? ` · P${node.source_pages.join(',')}` : ''}
                  </text>
                  <title>{`${node.title}\n${node.summary || '（无摘要）'}`}</title>
                </motion.g>
              )
            })}
          </g>
        </g>
      </svg>

      {/* 视口语义：缩放之后总要有办法回去 */}
      <div className="pointer-events-none absolute bottom-3 right-3 flex items-center gap-2">
        <span className="meta rounded-md bg-surface-1/90 px-2 py-1 shadow-e0">
          滚轮缩放 · 拖拽平移 · {Math.round(view.k * 100)}%
        </span>
        {!isFitted && (
          <button
            type="button"
            onClick={fitView}
            title="把整张图重新铺满画布"
            className="pointer-events-auto meta rounded-md bg-surface-1/90 px-2 py-1 shadow-e0 transition-colors hover:text-ink-1"
          >
            适配
          </button>
        )}
      </div>
    </div>
  )
}
