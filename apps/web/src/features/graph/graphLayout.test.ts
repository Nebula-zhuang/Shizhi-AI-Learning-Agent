/**
 * 图谱布局测试。
 *
 * 这些用例的价值在于：布局是**纯函数**，同样的输入永远得到同样的坐标。
 * 如果哪天有人把布局换成力导向算法，这些断言会立刻失败 ——
 * 而那正是需要停下来想清楚的一次变更（用户会找不到刚才看的那个知识点）。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import type { GraphEdge, GraphNode } from '../../api/graph.ts'
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  edgePath,
  edgeStyle,
  isDirected,
  layoutGraph,
  statusStroke,
  touchesNode,
} from './graphLayout.ts'

function node(
  id: number,
  heading: string[] | null,
  order: number,
  extra: Partial<GraphNode> = {},
): GraphNode {
  return {
    id,
    title: `节点${id}`,
    summary: '',
    difficulty: 2,
    importance: 4,
    confidence: 0.9,
    verify_status: 'unverified',
    heading_path: heading,
    source_pages: [1],
    order_index: order,
    depth: heading ? heading.length : 1,
    ...extra,
  }
}

function edge(id: number, from: number, to: number, type: GraphEdge['relation_type']): GraphEdge {
  return {
    id,
    from_kp_id: from,
    to_kp_id: to,
    relation_type: type,
    inverse_type: type,
    confidence: 0.9,
    source: 'heading_parent',
    evidence: '测试依据',
  }
}

// --------------------------------------------------------------------------- #
// 分带
// --------------------------------------------------------------------------- #
test('同一小节的知识点归入同一条带', () => {
  const layout = layoutGraph([
    node(1, ['3.1 概念'], 0),
    node(2, ['3.1 概念'], 1),
    node(3, ['3.2 状态'], 2),
  ])
  const byId = new Map(layout.nodes.map((n) => [n.id, n]))

  assert.equal(byId.get(1)!.band, byId.get(2)!.band)
  assert.notEqual(byId.get(2)!.band, byId.get(3)!.band)
  assert.equal(layout.bands.length, 2)
  assert.equal(layout.bands[0].label, '3.1 概念')
})

test('带按文档顺序自上而下排列', () => {
  const layout = layoutGraph([
    node(1, ['3.2 状态'], 5),
    node(2, ['3.1 概念'], 0),
  ])
  // order_index 更小的 3.1 应排在前面（y 更小）
  assert.equal(layout.bands[0].label, '3.1 概念')
  assert.equal(layout.bands[1].label, '3.2 状态')
  assert.ok(layout.bands[0].y < layout.bands[1].y)
})

test('带内按 order_index 从左到右排列', () => {
  const layout = layoutGraph([
    node(1, ['3.1 概念'], 5),
    node(2, ['3.1 概念'], 1),
    node(3, ['3.1 概念'], 3),
  ])
  const ordered = layout.nodes
    .slice()
    .sort((a, b) => a.x - b.x)
    .map((n) => n.id)
  // order_index 1 -> 3 -> 5，因此横轴顺序应是 2, 3, 1
  assert.deepEqual(ordered, [2, 3, 1])
})

test('没有章节信息的节点归入兜底带', () => {
  const layout = layoutGraph([node(1, null, 0), node(2, [], 1)])
  assert.equal(layout.bands.length, 1)
  assert.equal(layout.bands[0].label, '（未标注章节）')
  assert.equal(layout.bands[0].nodeCount, 2)
})

// --------------------------------------------------------------------------- #
// 换行：这条是为真实规模数据加的
// --------------------------------------------------------------------------- #
test('单带节点过多时折行，画布宽度保持有界', () => {
  // 12 个知识点同属一节 —— 真实课程资料里很常见
  const many = Array.from({ length: 12 }, (_, i) => node(i + 1, ['长小节'], i))
  const layout = layoutGraph(many)

  const ys = new Set(layout.nodes.map((n) => n.y))
  assert.ok(ys.size > 1, '应当折行而不是排成一整行')

  // 宽度不应随节点数无限增长：5 个一行，宽度上界约为 5 列
  const maxColumns = 5
  const widthBound = maxColumns * NODE_WIDTH + (maxColumns - 1) * 26 + 100
  assert.ok(layout.width <= widthBound, `画布过宽：${layout.width} > ${widthBound}`)
})

test('折行后同一行的节点不重叠', () => {
  const many = Array.from({ length: 8 }, (_, i) => node(i + 1, ['长小节'], i))
  const layout = layoutGraph(many)

  const rows = new Map<number, number[]>()
  for (const n of layout.nodes) {
    const bucket = rows.get(n.y)
    if (bucket) bucket.push(n.x)
    else rows.set(n.y, [n.x])
  }
  for (const xs of rows.values()) {
    xs.sort((a, b) => a - b)
    for (let i = 1; i < xs.length; i += 1) {
      assert.ok(xs[i] - xs[i - 1] >= NODE_WIDTH, '同行节点矩形相互覆盖')
    }
  }
})

test('不同节的节点纵向不重叠', () => {
  const layout = layoutGraph([node(1, ['3.1 概念'], 0), node(2, ['3.2 状态'], 1)])
  const first = layout.nodes.find((n) => n.id === 1)!
  const second = layout.nodes.find((n) => n.id === 2)!
  assert.ok(second.y >= first.y + NODE_HEIGHT + 20, '带间应留有足够间距')
})

test('画布尺寸能容纳全部节点', () => {
  const layout = layoutGraph([
    node(1, ['3.1 概念'], 0),
    node(2, ['3.1 概念'], 1),
    node(3, ['3.2 状态'], 2),
    node(4, ['3.2 状态'], 3),
  ])
  for (const n of layout.nodes) {
    assert.ok(n.x + n.width <= layout.width, `节点 ${n.id} 超出画布右边界`)
    assert.ok(n.y + n.height <= layout.height, `节点 ${n.id} 超出画布下边界`)
    assert.ok(n.x >= 0 && n.y >= 0)
  }
})

test('布局是确定性的：同样输入得到同样坐标', () => {
  const nodes = [node(1, ['3.1 概念'], 0), node(2, ['3.1 概念'], 1), node(3, ['3.2 状态'], 2)]
  const first = layoutGraph(nodes)
  const second = layoutGraph(nodes)

  assert.deepEqual(
    first.nodes.map((n) => [n.id, n.x, n.y]),
    second.nodes.map((n) => [n.id, n.x, n.y]),
  )
})

test('空输入返回安全的最小画布', () => {
  const layout = layoutGraph([])
  assert.deepEqual(layout.nodes, [])
  assert.deepEqual(layout.bands, [])
  assert.ok(layout.width > 0 && layout.height > 0)
})

// --------------------------------------------------------------------------- #
// 边的路径
// --------------------------------------------------------------------------- #
test('跨行边使用竖直曲线', () => {
  const layout = layoutGraph([node(1, ['3.1 概念'], 0), node(2, ['3.2 状态'], 1)])
  const from = layout.nodes.find((n) => n.id === 1)!
  const to = layout.nodes.find((n) => n.id === 2)!
  const path = edgePath(from, to)

  assert.ok(path.d.startsWith('M '), '路径应以 M 开头')
  assert.ok(path.d.includes('C '), '应使用贝塞尔曲线')
  assert.ok(path.labelY >= from.y && path.labelY <= to.y + NODE_HEIGHT)
})

test('同行边从侧面出发，不穿过节点', () => {
  const layout = layoutGraph([node(1, ['3.1 概念'], 0), node(2, ['3.1 概念'], 1)])
  const from = layout.nodes.find((n) => n.id === 1)!
  const to = layout.nodes.find((n) => n.id === 2)!
  const path = edgePath(from, to)

  // 起点 x 应贴在节点边缘而不是中心
  assert.ok(path.d.includes(`M ${from.x + from.width}`), '应从右边缘出发')
})

// --------------------------------------------------------------------------- #
// 视觉映射
// --------------------------------------------------------------------------- #
test('三种关系类型的线型互不相同', () => {
  const contains = edgeStyle('contains')
  const prerequisite = edgeStyle('prerequisite')
  const related = edgeStyle('related')

  // 颜色两两不同，且虚线样式可区分 —— 否则"能看到关系类型"就落空了
  const colors = new Set([contains.stroke, prerequisite.stroke, related.stroke])
  assert.equal(colors.size, 3)
  assert.equal(contains.dash, undefined)
  assert.ok(prerequisite.dash)
  assert.ok(related.dash)
  assert.notEqual(prerequisite.dash, related.dash)
})

test('只有有向关系才画箭头', () => {
  assert.equal(isDirected('contains'), true)
  assert.equal(isDirected('prerequisite'), true)
  // related 是无向的，画箭头会误导
  assert.equal(isDirected('related'), false)
})

test('校验状态映射到不同描边色', () => {
  // 实际会出现的只有两种：核对过（可信）与没核对过。
  // 【存疑】已被删除（软信号误报四成，见后端 CheckVerdict 文档），
  // 这里不再把它当成一个合法的状态来断言。
  const colors = ['trusted', 'unverified'].map(statusStroke)
  assert.equal(new Set(colors).size, 2, '可信与没核对必须颜色可分')

  // 未知状态回退到中性描边，不冒充任何一种判定。
  //
  // **断言"与 unverified 同档"而不是断言具体色值**：
  // 这些颜色现在走 CSS 变量（好让暗色主题能跟着变）。
  // 第一版这里写的是 '#d5cbbb'，改成令牌后就红了 ——
  // 那种断言测的是"我上次写的那串字符还在不在"，不是行为。
  const fallback = statusStroke('未知状态')
  assert.equal(fallback, statusStroke('unverified'), '未知状态应与"未校验"同档')

  // 唯一"有结论"的状态是 trusted，它不该撞上中性色 ——
  // 否则"看到绿色就知道这条核对过"就不成立了。
  // 注意不能把 unverified 算进来 —— 它本身就是中性档，撞上是应该的。
  assert.notEqual(statusStroke('trusted'), fallback, 'trusted 不该和"没结论"同色')
})

test('图谱配色走设计令牌，不写死色值', () => {
  // 写死色值的话，切到夜间模式它们会原地不动，
  // 成为整页里唯一"没跟上"的部分。
  const statuses = ['trusted', 'conflict', 'outdated', 'unverified', '未知']
  for (const status of statuses) {
    const color = statusStroke(status)
    assert.ok(color.startsWith('var(--color-'), `描边色应引用设计令牌，实际是 ${color}`)
  }

  for (const type of ['contains', 'prerequisite', 'related', 'unknown']) {
    const { stroke } = edgeStyle(type)
    assert.ok(stroke.startsWith('var(--color-'), `边色应引用设计令牌，实际是 ${stroke}`)
  }
})

test('touchesNode 正确识别相连的边', () => {
  const e = edge(1, 10, 20, 'contains')
  assert.equal(touchesNode(e, 10), true)
  assert.equal(touchesNode(e, 20), true)
  assert.equal(touchesNode(e, 30), false)
  assert.equal(touchesNode(e, null), false)
})

/* ══════════════════════════════════════════════════════════════════════
   按画布宽度决定每行几个 —— 修"图谱被缩放压小"的关键
   ══════════════════════════════════════════════════════════════════════ */

/**
 * 造一批同章节的节点。
 *
 * 放在同一个 heading_path 下，是为了让它们落在同一个分带里 ——
 * 只有同一带内行数够多，才能看出"每行几个"的差别。
 */
function sameBand(count: number): GraphNode[] {
  return Array.from({ length: count }, (_, index) => node(index + 1, ['3.1 概念'], index))
}

test('给了画布宽度时，布局不会宽过它', () => {
  // 实测过的真实场景：一屏 5 个节点时图宽 958px，而画布只有 646px，
  // 于是整张图被缩到 0.61 倍、字糊得看不清。
  const layout = layoutGraph(sameBand(10), { maxWidth: 646 })
  assert.ok(
    layout.width <= 646,
    `布局宽 ${layout.width} 超过了画布 646 —— 又会被缩放压小`,
  )
})

test('画布越窄，每行放得越少（图随之变窄）', () => {
  const wide = layoutGraph(sameBand(12), { maxWidth: 1200 })
  const narrow = layoutGraph(sameBand(12), { maxWidth: 520 })

  assert.ok(
    narrow.width < wide.width,
    `窄画布(${narrow.width})应当比宽画布(${wide.width})布得更窄`,
  )
  assert.ok(narrow.width <= 520 && wide.width <= 1200)
})

test('画布够宽时每行最多 5 个，不会无限拉长', () => {
  // 上限仍然生效：一屏 40 个节点不该摊成一条超宽的长条
  const layout = layoutGraph(sameBand(40), { maxWidth: 100000 })
  const perRow = 5
  const expected = perRow * NODE_WIDTH + (perRow - 1) * 26
  assert.ok(
    layout.width <= expected + 80 + 1,
    `每行节点数没有被上限约束住（宽 ${layout.width}）`,
  )
})

test('画布极窄时每行至少放 1 个，不会算出 0', () => {
  // 除法向下取整可能算出 0 —— 那会导致死循环或整批节点消失
  const layout = layoutGraph(sameBand(4), { maxWidth: 100 })
  assert.equal(layout.nodes.length, 4, '每个节点都必须有位置')
  const rows = new Set(layout.nodes.map((n) => n.row))
  assert.ok(rows.size >= 4, '挤到 1 列时应当各占一行')
})

test('不传宽度时用默认值，行为稳定', () => {
  const layout = layoutGraph(sameBand(12))
  assert.equal(layout.nodes.length, 12)
  assert.ok(layout.width > 0 && layout.height > 0)
})

test('节点不会因为换行而重叠', () => {
  const layout = layoutGraph(sameBand(10), { maxWidth: 646 })
  const byRow = new Map<number, typeof layout.nodes>()
  for (const n of layout.nodes) {
    const list = byRow.get(n.row) ?? []
    list.push(n)
    byRow.set(n.row, list)
  }
  for (const [, members] of byRow) {
    const sorted = [...members].sort((a, b) => a.x - b.x)
    for (let i = 1; i < sorted.length; i++) {
      assert.ok(
        sorted[i].x >= sorted[i - 1].x + sorted[i - 1].width,
        '同一行内节点重叠了',
      )
    }
  }
})
