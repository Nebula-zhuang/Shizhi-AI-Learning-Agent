/**
 * 我的资料 —— 数字书架。
 *
 * ## 为什么做成"书架"而不是"文件列表"
 *
 * 文件列表是**管理视角**：文件名、大小、状态、操作按钮。
 * 但学习者打开这一页想知道的是"**我手上有哪些书、我读到哪了**"。
 *
 * 所以这里每一份资料都渲染成一张**书脊卡**：左侧一道抽象的脊背（不是 PDF 缩略图 ——
 * 缩略图在小尺寸下只是一团灰，反而更乱），右边是书名、从中读出了多少知识点、
 * 读到什么程度、以及"继续读"。
 *
 * ## 上传过程为什么要有分步动画
 *
 * 解析一份 PDF 要几十秒。这段时间不显示进度，用户会以为卡死了 ——
 * 而显示一个假进度条更糟（它会在 99% 停住）。
 *
 * 这里的做法是**把后端真实的解析阶段翻译成四句人话**：
 * 读资料 → 提取知识 → 建立关系 → 准备就绪，当前阶段高亮、已完成的打勾。
 * 阶段名来自后端 `stage_detail`，不是前端编的。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'

import type { DocumentSummary } from '../../api/library'
import { MAX_UPLOAD_MB, checkUploadSize, formatBytes } from '../../api/library'
import { messageOf } from '../../api/http'
import { useLearning } from '../../app/LearningProvider'
import { Reveal } from '../../motion/primitives'
import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  IconArrowRight,
  IconFile,
  IconPlus,
  IconRefresh,
  IconTrash,
  Modal,
  Progress,
  SectionTitle,
  Skeleton,
  cn,
  useToast,
} from '../../ui'
import type { Tone } from '../../ui'
import { DocumentDetail } from './DocumentDetail'
import { useDocuments } from './useDocuments'
import { agoText } from '../learn/voice'

/* ------------------------------------------------------------------ 解析阶段 */

/**
 * 把后端的解析阶段归入四个可展示的步骤。
 *
 * **只做归类，不编造阶段** —— 每个 key 都对应后端真实存在的状态值。
 * 归不进去就落到当前这个（宁可少一个勾，也不要显示一个不存在的进度）。
 */
const STAGES = [
  { key: 'reading', label: '读资料', match: ['pending', 'parsing', 'chunking'] },
  { key: 'extracting', label: '提取知识', match: ['extracting'] },
  { key: 'building', label: '建立关系', match: ['relating', 'verifying'] },
  { key: 'ready', label: '准备就绪', match: ['ready'] },
] as const

function stageIndex(status: string): number {
  const found = STAGES.findIndex((stage) => (stage.match as readonly string[]).includes(status))
  return found < 0 ? 0 : found
}

function docTone(doc: DocumentSummary): Tone {
  if (doc.parse_status === 'ready') return 'moss'
  if (doc.parse_status === 'failed') return 'brick'
  return 'sienna'
}

function statusWords(doc: DocumentSummary): string {
  if (doc.parse_status === 'ready') return '读好了'
  if (doc.parse_status === 'failed') return '没读成'
  return '正在读'
}

/* ------------------------------------------------------------------ 拖拽上传区 */

/**
 * 文件选择器允许的扩展名。
 *
 * **必须与后端 `SUPPORTED_EXTENSIONS` 对齐。** 之前这里放了 `.docx`
 * 而后端根本没实现 —— 用户选完才被告知"不支持"，那是文案跑在了能力前面。
 * 现在两边一致；`.doc`（Word 97-2003 旧格式）不在列表里，
 * 后端会针对它给一句"请另存为 .docx"的具体提示。
 */
const ACCEPT_ATTR = '.pdf,.docx,.txt,.md,.markdown,.png,.jpg,.jpeg,.webp'


function Dropzone({
  onUpload,
  busy,
  progress,
  sizeError,
}: {
  onUpload: (file: File) => Promise<void>
  busy: boolean
  /** 上传进度 0–1；null 表示还没开始 */
  progress: number | null
  /** 就地显示的错误（大小超限等）。刻意用页内提示而不是弹窗 */
  sizeError: string | null
}) {
  const [dragging, setDragging] = useState(false)
  const [pending, setPending] = useState<string | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)

  const handleFiles = async (files: FileList | null) => {
    const file = files?.[0]
    if (!file) return

    // 先做大小检查。后端要收完整份才知道超限 ——
    // 传 500MB 再被拒是白等几十秒，这一步拦下来是秒回。
    const problem = checkUploadSize(file)
    if (problem) {
      setLocalError(problem)
      setPending(null)
      return
    }

    setLocalError(null)
    setPending(file.name)
    try {
      await onUpload(file)
    } finally {
      setPending(null)
    }
  }

  const uploading = pending !== null && progress !== null && progress < 1
  const problem = localError ?? sizeError

  return (
    <div className="space-y-2.5">
      <div
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault()
          setDragging(false)
          void handleFiles(event.dataTransfer.files)
        }}
        className={cn(
          'relative flex flex-col items-center justify-center overflow-hidden rounded-lg border border-dashed px-6 py-9 text-center transition-all duration-300',
          dragging
            ? 'border-moss bg-moss-soft/50'
            : problem
              ? 'border-brick-line'
              : 'border-line-strong hover:border-ink-4 hover:bg-surface-2',
        )}
        style={dragging ? { boxShadow: 'var(--glow-moss)' } : undefined}
      >
        {/* 上传进度直接铺成底色，不加进度条 —— 少一层视觉噪音 */}
        {uploading && (
          <span
            aria-hidden="true"
            className="absolute inset-y-0 left-0 bg-moss-soft/70 transition-[width] duration-200"
            style={{ width: `${Math.round((progress ?? 0) * 100)}%` }}
          />
        )}

        <input
          ref={input}
          type="file"
          accept={ACCEPT_ATTR}
          className="sr-only"
          onChange={(event) => void handleFiles(event.target.files)}
        />

        <motion.span
          animate={dragging ? { y: -3, scale: 1.06 } : { y: 0, scale: 1 }}
          transition={{ duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
          className={cn(
            'relative mb-3.5 inline-flex h-11 w-11 items-center justify-center rounded-full transition-colors duration-300',
            dragging ? 'bg-moss text-white' : 'bg-paper-sunken text-ink-3',
          )}
        >
          <IconPlus size={19} />
        </motion.span>

        <p className="relative text-sm text-ink-1">
          {uploading
            ? `正在传「${pending}」… ${Math.round((progress ?? 0) * 100)}%`
            : pending
              ? `正在传「${pending}」…`
              : dragging
                ? '松手就传上来'
                : '把讲义拖到这里'}
        </p>
        <p className="relative mt-1.5 text-xs leading-relaxed text-ink-3">
          支持 PDF、Word（.docx）、Markdown、纯文本与扫描件图片，
          单个文件最大 {MAX_UPLOAD_MB}MB。
          <br />
          老的 .doc 请先在 Word 里另存为 .docx。我读完会把它拆成知识点，再建好关系。
        </p>

        <Button
          variant="quiet"
          size="sm"
          className="relative mt-4"
          loading={busy || pending !== null}
          onClick={() => input.current?.click()}
        >
          或者选一个文件
        </Button>
      </div>

      {problem && (
        <p
          role="alert"
          className="rounded-md border border-brick-line bg-brick-soft px-3 py-2 text-xs leading-relaxed text-brick-ink"
        >
          {problem}
        </p>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ 解析进度 */

function ParseProgress({ doc }: { doc: DocumentSummary }) {
  const current = stageIndex(doc.parse_status)
  const failed = doc.parse_status === 'failed'

  return (
    <div className="mt-3">
      <div className="flex items-center gap-1.5">
        {STAGES.map((stage, index) => {
          const done = !failed && index < current
          const active = !failed && index === current
          return (
            <div key={stage.key} className="flex flex-1 flex-col gap-1.5">
              <div
                className={cn(
                  'h-0.5 rounded-full transition-colors duration-500',
                  failed
                    ? 'bg-brick-line'
                    : done
                      ? 'bg-moss'
                      : active
                        ? 'bg-moss/60'
                        : 'bg-line',
                )}
                style={active ? { animation: 'breathe 1.8s ease-in-out infinite' } : undefined}
              />
              <span
                className={cn(
                  'text-[10px] leading-none transition-colors duration-300',
                  active ? 'text-moss-ink' : done ? 'text-ink-3' : 'text-ink-4',
                )}
              >
                {stage.label}
              </span>
            </div>
          )
        })}
      </div>
      {doc.stage_detail && (
        <p className="mt-2.5 truncate text-2xs text-ink-3">{doc.stage_detail}</p>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ 书脊卡 */

function SpineCard({
  doc,
  index,
  active,
  onOpen,
  onLearn,
  onDelete,
}: {
  doc: DocumentSummary
  index: number
  active: boolean
  onOpen: () => void
  onLearn: () => void
  onDelete: () => void
}) {
  const tone = docTone(doc)
  const reading = doc.parse_status !== 'ready' && doc.parse_status !== 'failed'
  // 正在处理的文档删不掉（后端也会拒），按钮直接禁用而不是点了才报错
  const deletable = !reading

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: Math.min(index * 0.05, 0.3), ease: [0.16, 1, 0.3, 1] }}
      className={cn(
        'surface surface-interactive group relative flex gap-4 overflow-hidden p-4',
        active && 'border-moss-line',
      )}
      style={active ? { boxShadow: 'var(--glow-moss)' } : undefined}
    >
      {/* 删除入口。刻意**常驻但不抢眼**：
          藏在 hover 里的话，触屏和键盘用户根本找不到；
          而做成醒目的红色按钮，又会让整面书架显得像危险操作面板。
          所以是个默认很淡的图标，悬停才变成警示色。 */}
      <button
        type="button"
        onClick={onDelete}
        disabled={!deletable}
        aria-label={`删除 ${doc.file_name}`}
        title={deletable ? '删除这份资料' : '正在处理中，稍后可以删除'}
        className={cn(
          'absolute right-2.5 top-2.5 z-10 rounded-md p-1.5 transition-all duration-200',
          deletable
            ? 'text-ink-4 hover:bg-brick-soft hover:text-brick'
            : 'cursor-not-allowed text-ink-4/40',
        )}
      >
        <IconTrash size={14} />
      </button>

      {/* 书脊：抽象封面。用主色渐变 + 一道内阴影表示"这是一本书"，
          比放 PDF 首页缩略图稳定得多（缩略图在小尺寸下只是一团灰） */}
      <button
        type="button"
        onClick={onOpen}
        aria-label={`查看 ${doc.file_name}`}
        className="relative w-11 shrink-0 overflow-hidden rounded-sm"
        style={{
          background:
            'linear-gradient(160deg, var(--color-moss-soft) 0%, var(--color-paper-sunken) 100%)',
          boxShadow: 'inset -2px 0 3px -2px rgb(60 48 32 / 0.25)',
        }}
      >
        <span
          aria-hidden="true"
          className="absolute left-2 top-3 h-[2px] w-5 rounded-full bg-moss/40"
        />
        <span
          aria-hidden="true"
          className="absolute left-2 top-5.5 h-[2px] w-3.5 rounded-full bg-sienna/40"
        />
      </button>

      <div className="min-w-0 flex-1">
        <button type="button" onClick={onOpen} className="block w-full text-left">
          <p className="line-clamp-2 text-sm leading-snug text-ink-1">{doc.file_name}</p>
        </button>

        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <Badge tone={tone} dot>
            {statusWords(doc)}
          </Badge>
          <span className="meta">
            {doc.kp_count > 0 ? `${doc.kp_count} 个知识点` : '还没读出知识点'}
          </span>
          <span className="meta">{formatBytes(doc.file_size)}</span>
          <span className="meta">{agoText(doc.created_at)}</span>
        </div>

        {reading && (
          <>
            <Progress value={doc.progress / 100} tone="sienna" className="mt-3" />
            <ParseProgress doc={doc} />
          </>
        )}

        {doc.parse_status === 'failed' && (
          <p className="mt-2.5 text-2xs text-brick-ink">
            {doc.stage_detail || '解析中断了，可以重试一次。'}
          </p>
        )}

        {doc.parse_status === 'ready' && doc.kp_count > 0 && (
          <Button
            variant="quiet"
            size="sm"
            className="mt-3"
            iconEnd={<IconArrowRight size={13} />}
            onClick={onLearn}
          >
            学这份里的知识点
          </Button>
        )}
      </div>
    </motion.div>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function LibraryView() {
  const { documents, loading, error, upload, remove, reprocess, notice, setNotice } =
    useDocuments()
  const { setView } = useLearning()
  const toast = useToast()
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  //: 上传进度 0–1。300MB 的文件如果没有任何反馈，用户会以为卡死然后重复点击。
  const [progress, setProgress] = useState<number | null>(null)
  const [sizeError, setSizeError] = useState<string | null>(null)
  //: 待确认删除的文档。删除是不可逆的，**必须二次确认** ——
  //: 一份资料背后可能是几十次模型调用换来的知识点，误删代价高。
  const [pendingDelete, setPendingDelete] = useState<DocumentSummary | null>(null)
  const [deleting, setDeleting] = useState(false)

  const selected = useMemo(
    () => documents.find((doc) => doc.id === selectedId) ?? null,
    [documents, selectedId],
  )

  useEffect(() => {
    if (selectedId !== null && !documents.some((doc) => doc.id === selectedId)) {
      setSelectedId(null)
    }
  }, [documents, selectedId])

  const onUpload = useCallback(
    async (file: File) => {
      // 大小已经在 Dropzone 里查过一遍；这里再查是防止别的入口绕过
      const problem = checkUploadSize(file)
      if (problem) {
        setSizeError(problem)
        return
      }

      setBusy(true)
      setSizeError(null)
      setProgress(0)
      try {
        await upload(file, {
          onProgress: (ratio) => setProgress(ratio),
        })
        toast.success('收好了', '正在读它，进度在资料卡上看得到。')
      } catch (err) {
        // 取消不算失败，不弹错
        const text = messageOf(err)
        if (text !== '__aborted__') {
          setSizeError(text)
        }
      } finally {
        setBusy(false)
        setProgress(null)
      }
    },
    [upload, toast],
  )

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return
    setDeleting(true)
    try {
      await remove(pendingDelete.id)
      // 删掉的正好是详情里选中的那份，就一并收起详情
      if (selectedId === pendingDelete.id) setSelectedId(null)
      toast.success('已删除', `「${pendingDelete.file_name}」和它的知识点都清掉了。`)
      setPendingDelete(null)
    } catch (err) {
      toast.error('没删成', messageOf(err))
    } finally {
      setDeleting(false)
    }
  }, [pendingDelete, remove, selectedId, toast])

  const reading = documents.filter(
    (doc) => doc.parse_status !== 'ready' && doc.parse_status !== 'failed',
  )

  return (
    <div className="space-y-7">
      <header className="flex flex-wrap items-end justify-between gap-5">
        <Reveal>
          <p className="meta tracking-[0.14em]">我的资料</p>
          <h1 className="display mt-2.5">书架</h1>
          <p className="mt-3 max-w-[var(--size-reading)] text-sm leading-relaxed text-ink-2">
            {documents.length > 0
              ? `这里放着 ${documents.length} 份资料。我从里面读出了 ${documents.reduce(
                  (sum, doc) => sum + doc.kp_count,
                  0,
                )} 个知识点，学的时候就是围着它们转。`
              : '传一份讲义、教材或笔记上来，我读完会把它拆成知识点，再带着你学。'}
          </p>
        </Reveal>
      </header>

      {notice && (
        <div className="flex items-start justify-between gap-3 rounded-lg border border-line bg-surface-1 px-4 py-2.5">
          <p className="text-xs text-ink-2">{notice}</p>
          <button
            type="button"
            onClick={() => setNotice(null)}
            className="shrink-0 text-2xs text-ink-4 hover:text-ink-2"
          >
            知道了
          </button>
        </div>
      )}

      {error && <ErrorState message={error} />}

      <Dropzone onUpload={onUpload} busy={busy} progress={progress} sizeError={sizeError} />

      {/* 正在解析的单独提到前面 —— 这是用户此刻最关心的东西 */}
      {reading.length > 0 && (
        <section className="space-y-3">
          <SectionTitle
            eyebrow="正在处理"
            title={`${reading.length} 份资料正在读`}
          />
          <div className="grid gap-4 lg:grid-cols-2">
            {reading.map((doc, index) => (
              <SpineCard
                key={doc.id}
                doc={doc}
                index={index}
                active={selectedId === doc.id}
                onOpen={() => setSelectedId(doc.id)}
                onLearn={() => setView('learn')}
                onDelete={() => setPendingDelete(doc)}
              />
            ))}
          </div>
        </section>
      )}

      {/* 书架主体 */}
      <section className="space-y-3">
        <SectionTitle
          eyebrow="书架"
          title="读过的资料"
          action={
            <Button
              variant="ghost"
              size="sm"
              icon={<IconRefresh size={13} />}
              onClick={() => void reprocess(selectedId ?? documents[0]?.id ?? 0)}
              disabled={!selectedId}
            >
              重新读这份
            </Button>
          }
        />

        {loading ? (
          <div className="grid gap-4 lg:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="surface flex gap-4 p-4">
                <Skeleton width={44} height={62} />
                <div className="flex-1 space-y-2.5">
                  <Skeleton width="72%" height={14} />
                  <Skeleton width="46%" height={12} />
                  <Skeleton width="58%" height={12} />
                </div>
              </div>
            ))}
          </div>
        ) : documents.filter((doc) => doc.parse_status === 'ready' || doc.parse_status === 'failed')
            .length > 0 ? (
          <div className="grid gap-4 lg:grid-cols-2">
            {documents
              .filter((doc) => doc.parse_status === 'ready' || doc.parse_status === 'failed')
              .map((doc, index) => (
                <SpineCard
                  key={doc.id}
                  doc={doc}
                  index={index}
                  active={selectedId === doc.id}
                  onOpen={() => setSelectedId(doc.id)}
                  onLearn={() => {
                    setView('learn')
                  }}
                  onDelete={() => setPendingDelete(doc)}
                />
              ))}
          </div>
        ) : (
          reading.length === 0 && (
            <EmptyState
              icon={<IconFile size={20} />}
              title="书架还是空的"
              description="把上面那份讲义拖进来就行。我会先读一遍，再拆出知识点，之后你就能让助教带着你学了。"
            />
          )
        )}
      </section>

      {/* 资料详情 */}
      <AnimatePresence>
        {selected && (
          <motion.section
            key={selected.id}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 6 }}
            transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="space-y-3"
          >
            <SectionTitle
              eyebrow="这份资料"
              title={selected.file_name}
              action={
                <Button
                  variant="ghost"
                  size="sm"
                  icon={<IconTrash size={13} />}
                  onClick={() => setPendingDelete(selected)}
                >
                  删除
                </Button>
              }
            />
            <DocumentDetail doc={selected} onReprocess={reprocess} onDelete={remove} />
          </motion.section>
        )}
      </AnimatePresence>

      {/* 删除确认。
          用弹层而不是"点了就删 + 撤销提示"：这个产品里一份资料背后是
          **几十次模型调用换来的知识点**，误删的代价比多一次点击高得多。
          文案里也把"会一起删掉什么"说清楚，而不是干问一句"确定吗"。 */}
      <Modal
        open={pendingDelete !== null}
        onClose={() => {
          if (!deleting) setPendingDelete(null)
        }}
        title="删掉这份资料？"
        description={
          pendingDelete
            ? `「${pendingDelete.file_name}」以及从它读出的 ${pendingDelete.kp_count} 个知识点、相关的关系与索引都会一起清掉。`
            : ''
        }
        footer={
          <>
            <Button variant="quiet" onClick={() => setPendingDelete(null)} disabled={deleting}>
              先留着
            </Button>
            <Button variant="danger" loading={deleting} onClick={() => void confirmDelete()}>
              确认删除
            </Button>
          </>
        }
      >
        <p className="text-xs leading-relaxed text-ink-3">
          这个操作不能撤销。之后再想看，需要重新上传一次，也要重新花时间读一遍 ——
          如果只是想重新解析，用「重新读这份」就够了。
        </p>
      </Modal>
    </div>
  )
}
