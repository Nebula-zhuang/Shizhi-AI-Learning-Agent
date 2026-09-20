/**
 * 资料库接口封装（P1）。
 *
 * 对应后端 app/api/routes/documents.py 与 knowledge.py。
 */

import { deleteJson, getJson, postJson, uploadFile } from './http'
import type { UploadOptions } from './http'

export type ParseStatus =
  | 'pending'
  | 'parsing'
  | 'chunking'
  | 'extracting'
  | 'ready'
  | 'failed'

/** 处于中间态：需要继续轮询 */
export const IN_PROGRESS: ParseStatus[] = ['pending', 'parsing', 'chunking', 'extracting']

export interface DocumentSummary {
  id: number
  file_name: string
  file_type: string
  file_size: number
  parse_status: ParseStatus
  progress: number
  stage_detail: string | null
  page_count: number
  char_count: number
  image_count: number
  chunk_count: number
  kp_count: number
  created_at: string
}

export interface ParseWarning {
  code: string
  page_no?: number | null
  message: string
}

export interface DocumentDetail extends DocumentSummary {
  parse_error: string | null
  warnings: ParseWarning[]
}

export interface DocumentStatus {
  document_id: number
  parse_status: ParseStatus
  progress: number
  stage_detail: string | null
  chunk_count: number
  kp_count: number
  parse_error: string | null
  warning_count: number
}

export interface UploadResponse {
  document: DocumentSummary
  dedup: boolean
  message: string
}

export interface DocumentListResponse {
  items: DocumentSummary[]
  total: number
}

export interface KnowledgePointSummary {
  id: number
  document_id: number
  title: string
  summary: string
  difficulty: number
  importance: number
  confidence: number
  verify_status: string
  tags: string[] | null
  heading_path: string[] | null
  source_pages: number[] | null
  order_index: number
}

export interface SourceChunk {
  chunk_index: number
  page_start: number
  page_end: number
  block_type: string
  content: string
}

export interface KnowledgePointDetail extends KnowledgePointSummary {
  details: string
  key_points: string[] | null
  source_chunk_indexes: number[] | null
  sources: SourceChunk[]
  created_at: string
}

export interface KnowledgePointListResponse {
  items: KnowledgePointSummary[]
  total: number
}

// --------------------------------------------------------------------------- #
export function listDocuments(limit = 50, offset = 0): Promise<DocumentListResponse> {
  return getJson<DocumentListResponse>(`/api/documents?limit=${limit}&offset=${offset}`)
}

/**
 * 单文件上限（MB）。**必须与后端 `.env` 的 `MAX_UPLOAD_MB` 一致。**
 *
 * 前端也存一份的理由不是"双重保险"，是**体验**：
 * 后端要等整个文件收完才知道超限，传一个 500MB 的文件再被拒，用户白等几十秒。
 * 前端一开始就能拦下来，立刻给话。
 *
 * 不一致的后果：前端放行、后端 413 —— 用户会以为程序坏了。
 */
export const MAX_UPLOAD_MB = 300

const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

/**
 * 上传前的大小检查。返回一句给用户看的话；通过则返回 `null`。
 *
 * **刻意不自动压缩**：静默改小用户的文件比拒绝更糟 ——
 * 他以为传上去的是原件，实际解析结果会莫名其妙地不完整。
 */
export function checkUploadSize(file: File): string | null {
  if (file.size === 0) return '这个文件是空的。'
  if (file.size > MAX_UPLOAD_BYTES) {
    const actual = (file.size / 1024 / 1024).toFixed(0)
    return `这个文件 ${actual}MB，超过了 ${MAX_UPLOAD_MB}MB 的上限。请压缩或拆分后再传。`
  }
  return null
}

export function uploadDocument(
  file: File,
  options: UploadOptions = {},
): Promise<UploadResponse> {
  return uploadFile<UploadResponse>('/api/documents', file, options)
}

export function getDocument(id: number): Promise<DocumentDetail> {
  return getJson<DocumentDetail>(`/api/documents/${id}`)
}

export function getDocumentStatus(id: number): Promise<DocumentStatus> {
  return getJson<DocumentStatus>(`/api/documents/${id}/status`)
}

export function reprocessDocument(id: number): Promise<UploadResponse> {
  return postJson<UploadResponse>(`/api/documents/${id}/reprocess`)
}

export function deleteDocument(id: number): Promise<{ ok: boolean; message: string }> {
  return deleteJson<{ ok: boolean; message: string }>(`/api/documents/${id}`)
}

export function listKnowledgePoints(
  documentId: number,
  options: { order?: 'document' | 'difficulty' | 'importance'; limit?: number } = {},
): Promise<KnowledgePointListResponse> {
  const order = options.order ?? 'document'
  const limit = options.limit ?? 200
  return getJson<KnowledgePointListResponse>(
    `/api/documents/${documentId}/knowledge-points?order=${order}&limit=${limit}`,
  )
}

export function getKnowledgePoint(id: number): Promise<KnowledgePointDetail> {
  return getJson<KnowledgePointDetail>(`/api/knowledge-points/${id}`)
}

/** 提取出的内嵌图片的访问地址 */
export function documentImageUrl(documentId: number, fileName: string): string {
  return `/api/documents/${documentId}/images/${encodeURIComponent(fileName)}`
}

// --------------------------------------------------------------------------- #
export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / 1024 ** index
  return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}
