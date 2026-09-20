import { StatusBadge } from '../../components/StatusBadge'
import {
  IN_PROGRESS,
  formatBytes,
  formatDateTime,
  type DocumentSummary,
} from '../../api/library'

interface DocumentListProps {
  documents: DocumentSummary[]
  selectedId: number | null
  onSelect: (id: number) => void
}

const FILE_ICON: Record<string, string> = {
  pdf: 'PDF',
  txt: 'TXT',
  md: 'MD',
  image: 'IMG',
}

/** 文档列表。进行中的文档显示进度条，让"后台在干活"这件事可见。 */
export function DocumentList({ documents, selectedId, onSelect }: DocumentListProps) {
  if (documents.length === 0) {
    return (
      <p className="px-1 py-6 text-center text-xs text-ink-4">
        还没有资料，先上传一份试试
      </p>
    )
  }

  return (
    <ul className="space-y-2">
      {documents.map((doc) => {
        const running = IN_PROGRESS.includes(doc.parse_status)
        const active = doc.id === selectedId
        return (
          <li key={doc.id}>
            <button
              type="button"
              onClick={() => onSelect(doc.id)}
              className={[
                'w-full rounded-xl border px-3 py-2.5 text-left transition',
                active
                  ? 'border-moss-line bg-moss-soft/60'
                  : 'border-line bg-paper-raised hover:border-line-strong hover:bg-paper-sunken',
              ].join(' ')}
            >
              <div className="flex items-start justify-between gap-2">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="shrink-0 rounded bg-paper-sunken px-1.5 py-0.5 text-2xs font-medium text-ink-3">
                    {FILE_ICON[doc.file_type] ?? doc.file_type.toUpperCase()}
                  </span>
                  <span className="truncate text-sm text-ink-1" title={doc.file_name}>
                    {doc.file_name}
                  </span>
                </span>
                <StatusBadge status={doc.parse_status} />
              </div>

              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-ink-4">
                <span>{formatBytes(doc.file_size)}</span>
                {doc.page_count > 0 && <span>{doc.page_count} 页</span>}
                {doc.kp_count > 0 && (
                  <span className="text-moss">{doc.kp_count} 个知识点</span>
                )}
                <span>{formatDateTime(doc.created_at)}</span>
              </div>

              {running && (
                <div className="mt-2">
                  <div className="h-1 w-full overflow-hidden rounded-full bg-paper-sunken">
                    <div
                      className="h-full rounded-full bg-moss transition-all duration-500"
                      style={{ width: `${Math.max(doc.progress, 3)}%` }}
                    />
                  </div>
                  <p className="mt-1 truncate text-xs text-ink-4">{doc.stage_detail}</p>
                </div>
              )}
            </button>
          </li>
        )
      })}
    </ul>
  )
}
