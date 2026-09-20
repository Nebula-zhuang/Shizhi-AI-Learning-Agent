import { useRef, useState } from 'react'

const ACCEPT = '.pdf,.txt,.md,.markdown,.png,.jpg,.jpeg,.webp'

interface UploadPanelProps {
  onUpload: (file: File) => Promise<unknown>
}

/**
 * 上传区。支持点击选择与拖拽。
 *
 * 上传是"接收即返回"的：后端立刻返回 202，解析与抽取在后台进行，
 * 因此这里的成功提示只代表"已接收"，真实进度看列表里的状态徽标。
 */
export function UploadPanel({ onUpload }: UploadPanelProps) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleFiles = async (files: FileList | null) => {
    const file = files?.[0]
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      await onUpload(file)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <div className="space-y-2">
      <div
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          void handleFiles(e.dataTransfer.files)
        }}
        onClick={() => inputRef.current?.click()}
        className={[
          'cursor-pointer rounded-xl border border-dashed px-4 py-6 text-center transition',
          dragging
            ? 'border-moss bg-moss-soft'
            : 'border-line-strong bg-paper-raised hover:border-moss-line hover:bg-paper-sunken',
          busy ? 'pointer-events-none opacity-60' : '',
        ].join(' ')}
      >
        <p className="text-sm text-ink-2">
          {busy ? '正在上传…' : '拖拽文件到此处，或点击选择'}
        </p>
        <p className="mt-1 text-xs text-ink-4">
          支持 PDF / TXT / MD / PNG / JPG，单文件 ≤30MB、≤50 页
        </p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          className="hidden"
          onChange={(e) => void handleFiles(e.target.files)}
        />
      </div>

      {error && (
        <div className="rounded-lg border border-brick-line bg-brick-soft px-3 py-2 text-xs text-brick-ink">
          {error}
        </div>
      )}
    </div>
  )
}
