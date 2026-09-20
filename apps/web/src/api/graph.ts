/**
 * 知识图谱与可信度校验接口封装（P2）。
 *
 * 对应后端 app/api/routes/graph.py。
 * HTTP 细节全部复用 P1 的 http.ts，这里只描述资源与类型。
 */

import { getJson, postJson } from './http'
import type { KnowledgePointDetail } from './library'

// --------------------------------------------------------------------------- #
// 图谱
// --------------------------------------------------------------------------- #
export type RelationType = 'prerequisite' | 'related' | 'contains' | 'belongs_to'

export interface GraphNode {
  id: number
  title: string
  summary: string
  difficulty: number
  importance: number
  confidence: number
  verify_status: string
  heading_path: string[] | null
  source_pages: number[] | null
  order_index: number
  /** 章节深度，前端据此分层排布 */
  depth: number
}

export interface GraphEdge {
  id: number
  from_kp_id: number
  to_kp_id: number
  relation_type: RelationType
  /** 反向视角的类型：contains 的反向是 belongs_to */
  inverse_type: RelationType
  confidence: number
  /** 构建依据（规则名），不是随机连边 */
  source: string
  /** 人类可读的依据说明 */
  evidence: string
}

export interface GraphStats {
  node_count: number
  edge_count: number
  by_type: Record<string, number>
  max_depth: number
  isolated_nodes: number
}

export interface GraphResponse {
  document_id: number
  document_name: string
  nodes: GraphNode[]
  edges: GraphEdge[]
  stats: GraphStats
}

export interface RelationBuildResponse {
  ok: boolean
  document_id: number
  message: string
  stats: Record<string, unknown>
}

// --------------------------------------------------------------------------- #
// 可信度校验
// --------------------------------------------------------------------------- #
export type CheckType = 'rule' | 'model' | 'web'
export type CheckVerdict = 'passed' | 'unsupported' | 'skipped' | 'error'

export interface CheckItem {
  id: number
  check_type: CheckType
  verdict: CheckVerdict
  confidence: number
  reason: string
  evidence: Record<string, unknown> | null
  source_urls: string[] | null
  engine: string
  created_at: string
}

export interface CheckGroup {
  check_type: CheckType
  latest: CheckItem | null
  history: CheckItem[]
  count: number
}

export interface KnowledgeCheckResponse {
  kp_id: number
  title: string
  verify_status: string
  groups: CheckGroup[]
  total: number
}

export interface VerifyStatusResponse {
  document_id: number
  state: 'idle' | 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  progress: number
  detail: string
  total_points: number
  checked_points: number
  by_status: Record<string, number>
  stats: Record<string, unknown>
}

export interface VerifyCapabilities {
  verify_enabled: boolean
  web_verify_enabled: boolean
  web_provider: string
  tavily_configured: boolean
  web_verify_effective: boolean
  model_importance_threshold: number
  web_max_per_document: number
}

export interface PointRelationItem {
  id: number
  /** 以当前知识点为视角的方向 */
  direction: 'in' | 'out'
  relation_type: RelationType
  peer_id: number
  peer_title: string
  confidence: number
  source: string
  evidence: string
}

// --------------------------------------------------------------------------- #
// 接口
// --------------------------------------------------------------------------- #
export function getGraph(documentId: number): Promise<GraphResponse> {
  return getJson<GraphResponse>(`/api/documents/${documentId}/graph`)
}

export function buildRelations(documentId: number): Promise<RelationBuildResponse> {
  return postJson<RelationBuildResponse>(`/api/documents/${documentId}/relations`)
}

export function startVerify(documentId: number): Promise<{
  document_id: number
  accepted: boolean
  message: string
}> {
  return postJson(`/api/documents/${documentId}/verify`)
}

export function getVerifyStatus(documentId: number): Promise<VerifyStatusResponse> {
  return getJson<VerifyStatusResponse>(`/api/documents/${documentId}/verify/status`)
}

export function getVerifyCapabilities(): Promise<VerifyCapabilities> {
  return getJson<VerifyCapabilities>('/api/verify/capabilities')
}

export function getKnowledgeChecks(kpId: number): Promise<KnowledgeCheckResponse> {
  return getJson<KnowledgeCheckResponse>(`/api/knowledge-points/${kpId}/checks`)
}

export function getPointRelations(kpId: number): Promise<{
  kp_id: number
  title: string
  total: number
  items: PointRelationItem[]
}> {
  return getJson(`/api/knowledge-points/${kpId}/relations`)
}

// --------------------------------------------------------------------------- #
// 展示文案映射
//
// 后端只返回原始枚举值（与 P1 的做法一致），中文文案由前端负责 ——
// 这样改文案不必动后端，新增枚举也不会在两边各留一处硬编码。
// --------------------------------------------------------------------------- #
export const RELATION_LABEL: Record<RelationType, string> = {
  prerequisite: '前置',
  related: '相关',
  contains: '包含',
  belongs_to: '属于',
}

/**
 * 可信度状态的展示名。
 *
 * 全应用**只此一份** —— 知识点卡片与图谱图例都引它，
 * 避免同一个状态在两处叫不同名字（曾经卡片说"未核对"、图例说"未校验"）。
 * 用词原则：说人话，不用"校验"这种内部动词。
 */
export const VERIFY_STATUS_LABEL: Record<string, string> = {
  unverified: '还没核对',
  trusted: '可信',
  // ⚠️ 曾经的 `suspect`（存疑）已被删除。理由见后端 `CheckVerdict` 的文档：
  // 它的产出规则是软信号，实测标出了 40% 的知识点 ——
  // 一个每两三条就命中一条的警示标签，用户学会的是无视它，
  // 于是真正有问题的少数几条也一起被无视了。
  //
  // `conflict` / `outdated` 是**保留位**：当前不产出，
  // 但保留映射以免历史数据（或将来启用时）显示成一个裸的状态码。
  conflict: '有出入',
  outdated: '可能过时',
}

/**
 * 三种核查办法的展示名。
 *
 * 刻意不用"规则校验 / 模型自评 / 联网核验"这种内部叫法 ——
 * 学习者不知道什么是"模型自评"。换成"我用了哪三种办法查"。
 */
export const CHECK_TYPE_LABEL: Record<CheckType, string> = {
  rule: '比对原文',
  model: '独立再判断',
  web: '上网查证',
}

/** 核查结论的展示名 —— 说结果，不说状态码 */
export const CHECK_VERDICT_LABEL: Record<CheckVerdict, string> = {
  passed: '没问题',
  // 原来的 `suspicious: '存疑'` 已并入这里 —— 措辞刻意克制：
  // "原文里没写"是在说**我们查到的**，不是指控这条有问题。
  unsupported: '原文里没写',
  skipped: '这次没查',
  error: '没查成',
}

/** 依据规则名 → 中文说明。让用户看懂"这条边是怎么来的"。 */
export const RELATION_SOURCE_LABEL: Record<string, string> = {
  heading_parent: '章节层级',
  title_contains: '标题包含',
  shared_chunk: '同源原文块',
  same_section: '同小节相邻',
  order_heuristic: '章节顺序',
  llm_confirmed: '模型确认',
}

export type { KnowledgePointDetail }
