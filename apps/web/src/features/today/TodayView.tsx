/**
 * 今天 —— 学习空间首页。
 *
 * ## 这不是 Dashboard，是"进门第一眼"
 *
 * 判据很简单：**如果把它所有数字都删掉，页面还说不说得出话？**
 *
 * v1 的首页是"6 个待复习 / 追踪 6 个知识点 / 薄弱 6 个"——
 * 删掉数字就什么都不剩了，那是看板。
 * v2 首页的每一句话即使不报数字也成立：
 *
 *   「上次你在这儿卡住了，今天我们把它彻底弄清楚。」
 *
 * ## 页面结构（按"进门后的视线顺序"排）
 *
 *   1. 问候 + 今天该干什么（一句话，自然语言）
 *   2. 接着学 —— 一个主行动，指向最该学的那个知识点
 *   3. 该复习了 + 学习情况（左内容右状态）
 *   4. 最近读的资料（书架横排）
 *
 * 刻意**不做**的：指标卡墙、进度仪表盘、欢迎弹窗、每日签到。
 */

import { useEffect, useMemo, useState } from 'react'
import { motion } from 'motion/react'

import { listDocuments, type DocumentSummary } from '../../api/library'
import { useAuth } from '../../app/AuthProvider'
import { useLearning } from '../../app/LearningProvider'
import { Reveal } from '../../motion/primitives'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  IconArrowRight,
  IconArrowUpRight,
  IconBookmark,
  IconClock,
  IconFile,
  IconLibrary,
  IconMap,
  IconSpark,
  Ring,
  SectionTitle,
  Skeleton,
  SkeletonWorkspace,
} from '../../ui'
import { recommendNext } from '../learn/recommend'
import {
  agoText,
  greeting,
  lastTrouble,
  masteryMark,
  pointStatusWords,
  progressSentence,
} from '../learn/voice'

/* ------------------------------------------------------------------ 数据 */

function useRecentDocuments() {
  const [documents, setDocuments] = useState<DocumentSummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let alive = true
    listDocuments()
      .then((data) => {
        if (alive) setDocuments(data.items.slice(0, 4))
      })
      .catch(() => {
        /* 首页不因资料接口失败而整页报错 —— 那是次要信息 */
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  return { documents, loading }
}

function todayLabel(): string {
  const now = new Date()
  const week = ['日', '一', '二', '三', '四', '五', '六'][now.getDay()]
  return `${now.getFullYear()} 年 ${now.getMonth() + 1} 月 ${now.getDate()} 日 · 星期${week}`
}

/* ------------------------------------------------------------------ 局部组件 */

/** 待复习的书签卡。左缘探出一小截暖色标签 —— 品牌的记忆点 */
function DueBookmark({
  title,
  status,
  trouble,
  when,
  index,
  onStart,
}: {
  title: string
  status: string
  trouble: string
  when: string
  index: number
  onStart: () => void
}) {
  return (
    <motion.button
      type="button"
      onClick={onStart}
      initial={{ opacity: 0, x: -6 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.36, delay: 0.05 + index * 0.06, ease: [0.16, 1, 0.3, 1] }}
      className="surface surface-interactive bookmark group flex w-full items-start gap-3 px-4 py-3 text-left"
    >
      <span className="mt-0.5 shrink-0 text-sienna/70">
        <IconBookmark size={14} />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm text-ink-1">{title}</span>
        <span className="mt-1 block text-2xs leading-relaxed text-ink-3">
          {status}
          {trouble && ` · ${trouble}`}
          {when && ` · ${when}`}
        </span>
      </span>
      <span className="mt-1 shrink-0 text-ink-4 opacity-0 transition-opacity duration-200 group-hover:opacity-100">
        <IconArrowRight size={14} />
      </span>
    </motion.button>
  )
}

/** 资料缩略：不用真封面，用"书脊 + 文件名"的抽象表示 —— 比截一页 PDF 干净 */
function DocTile({ doc, index }: { doc: DocumentSummary; index: number }) {
  const tone =
    doc.parse_status === 'ready' ? 'moss' : doc.parse_status === 'failed' ? 'brick' : 'sienna'

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, delay: 0.06 * index, ease: [0.16, 1, 0.3, 1] }}
      className="surface surface-interactive group flex gap-3 p-3.5"
    >
      {/* 书脊：用资料的色相做一个抽象封面，比缩略图更稳定也更快 */}
      <span
        aria-hidden="true"
        className="w-8 shrink-0 rounded-sm bg-gradient-to-b from-moss-soft to-paper-sunken"
        style={{ boxShadow: 'inset -1px 0 0 var(--color-line)' }}
      />
      <div className="min-w-0 flex-1">
        <p className="line-clamp-2 text-xs leading-snug text-ink-1">{doc.file_name}</p>
        <p className="meta mt-1.5 truncate">
          {doc.kp_count > 0 ? `${doc.kp_count} 个知识点` : '还没读出知识点'}
          {doc.created_at && ` · ${agoText(doc.created_at)}`}
        </p>
        <Badge tone={tone} dot className="mt-2">
          {doc.parse_status === 'ready'
            ? '读好了'
            : doc.parse_status === 'failed'
              ? '没读成'
              : '正在读'}
        </Badge>
      </div>
    </motion.div>
  )
}

/* ------------------------------------------------------------------ 页面 */

export function TodayView() {
  const { user } = useAuth()
  const { dashboard, startLearning, setView } = useLearning()
  const { documents, loading: docsLoading } = useRecentDocuments()

  const next = useMemo(() => recommendNext(dashboard), [dashboard])
  const due = dashboard?.due_reviews ?? []
  const weak = dashboard?.weak_points ?? []

  // 数据还没回来：给骨架屏，不要给"空页面"（空页面会被误读成"我没学过东西"）
  if (!dashboard) {
    return (
      <div className="space-y-7">
        <div>
          <Skeleton width="9rem" height={12} />
          <Skeleton className="mt-3" width="58%" height={34} />
        </div>
        <SkeletonWorkspace />
      </div>
    )
  }

  const overview = dashboard.overview
  const hasHistory = overview.tracked > 0

  return (
    <div className="space-y-8">
      {/* ═══════════════════════════════════ 问候 + 今天该干什么 */}
      <header>
        <Reveal>
          <p className="meta tracking-[0.14em]">{todayLabel()}</p>
          <h1 className="display mt-2.5">
            {greeting()}，{user?.display_name ?? '同学'}
          </h1>
          <p className="mt-3 max-w-[var(--size-reading)] text-sm leading-relaxed text-ink-2">
            {progressSentence(overview)}
            {due.length > 0 &&
              `　今天有 ${due.length} 个知识点到了该复习的时候，我从最该补的那个开始。`}
          </p>
        </Reveal>
      </header>

      {/* ═══════════════════════════════════ 接着学（唯一的主行动） */}
      {next && (
        <Reveal delay={0.08}>
          <Card padded={false} className="relative overflow-hidden">
            {/* 主色光池：把视线钉在这一块上。位置与强度都压得很低 */}
            <span
              aria-hidden="true"
              className="pointer-events-none absolute -right-16 -top-24 h-56 w-56 rounded-full opacity-60"
              style={{
                background:
                  'radial-gradient(circle, var(--color-moss-soft) 0%, transparent 70%)',
              }}
            />
            <div className="relative flex flex-wrap items-center justify-between gap-5 p-6">
              <div className="min-w-0 flex-1">
                <p className="meta mb-2 tracking-[0.14em]">接着学</p>
                <h2 className="text-2xl">{next.title}</h2>
                <p className="mt-2 max-w-[36rem] text-sm leading-relaxed text-ink-2">
                  {next.reason}
                </p>
              </div>
              <Button
                variant="primary"
                icon={<IconSpark size={15} />}
                onClick={() =>
                  void startLearning({ kpId: next.kpId, title: next.title, documentId: null })
                }
                className="shrink-0"
              >
                开始学
              </Button>
            </div>
          </Card>
        </Reveal>
      )}

      {/* ═══════════════════════════════════ 该复习了 + 学习情况 */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_308px]">
        <section className="min-w-0 space-y-3">
          <SectionTitle
            eyebrow="该复习了"
            title={
              due.length > 0 ? (
                <>
                  这几处上次没通，趁还记得住先过一遍
                  <span className="ml-2 align-middle text-sm font-normal text-ink-3">
                    {due.length} 个
                  </span>
                </>
              ) : (
                '眼下没有到期的复习'
              )
            }
          />

          {due.length > 0 ? (
            <div className="space-y-2">
              {due.slice(0, 5).map((item, index) => (
                <DueBookmark
                  key={item.knowledge_point_id}
                  index={index}
                  title={item.title}
                  status={masteryMark(item)}
                  trouble={lastTrouble(item)}
                  when={item.due ? '该看了' : item.next_review_at ? '还没到点' : ''}
                  onStart={() =>
                    void startLearning({
                      kpId: item.knowledge_point_id,
                      title: item.title,
                      documentId: null,
                    })
                  }
                />
              ))}
            </div>
          ) : (
            <EmptyState
              title={hasHistory ? '今天的复习都清空了' : '还没有开始的记录'}
              description={
                hasHistory
                  ? '按你现在的节奏，暂时没有需要回头补的地方。可以往前学新的。'
                  : '随便挑一个知识点开始，我会先讲清楚，再问你几个问题看看是不是真懂了。'
              }
              action={
                <Button variant="quiet" onClick={() => setView('library')}>
                  去看看资料
                </Button>
              }
            />
          )}
        </section>

        {/* ───────────────────────────────── 右栏：学习情况 */}
        <aside className="space-y-5">
          <Card>
            <p className="meta mb-3 tracking-[0.14em]">学习情况</p>

            <div className="flex items-center gap-4">
              <Ring value={overview.mastered / Math.max(1, overview.tracked)} size={52} stroke={4}>
                <span className="font-serif text-sm text-moss">
                  {overview.mastered}
                  <span className="text-ink-4">/{overview.tracked}</span>
                </span>
              </Ring>
              <div className="min-w-0">
                <p className="text-sm text-ink-1">
                  {overview.mastered > 0 ? '已经稳住了一些' : '正在打地基'}
                </p>
                <p className="mt-1 text-2xs leading-relaxed text-ink-3">
                  圆心是已经掌握的知识点数，一共开了 {overview.tracked} 个。
                </p>
              </div>
            </div>

            <dl className="mt-5 space-y-2.5 border-t border-line pt-4">
              <div className="flex items-baseline justify-between gap-3">
                <dt className="text-xs text-ink-3">还需要补一补的</dt>
                <dd className="text-xs text-ink-1">
                  {weak.length > 0 ? `${weak.length} 个` : '暂时没有'}
                </dd>
              </div>
              <div className="flex items-baseline justify-between gap-3">
                <dt className="text-xs text-ink-3">我的讲法偏好</dt>
                <dd className="text-xs text-ink-1">{dashboard.profile?.style_label ?? '均衡'}</dd>
              </div>
            </dl>

            <Button
              variant="quiet"
              size="sm"
              className="mt-5 w-full"
              icon={<IconMap size={14} />}
              onClick={() => setView('map')}
            >
              看知识地图
            </Button>
          </Card>

          {/* 最近在学习 */}
          {weak.length > 0 && (
            <Card>
              <p className="meta mb-3 tracking-[0.14em]">最近卡住的地方</p>
              <ul className="space-y-2.5">
                {weak.slice(0, 3).map((item) => (
                  <li key={item.knowledge_point_id} className="flex gap-2.5">
                    <span
                      aria-hidden="true"
                      className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-sienna"
                    />
                    <div className="min-w-0">
                      <p className="truncate text-xs text-ink-1">{item.title}</p>
                      <p className="mt-0.5 text-2xs leading-relaxed text-ink-3">
                        {pointStatusWords(item)} · {lastTrouble(item)}
                      </p>
                    </div>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </aside>
      </div>

      {/* ═══════════════════════════════════ 最近读的资料 */}
      <section className="space-y-3">
        <SectionTitle
          eyebrow="最近读的"
          title="你的资料"
          action={
            <button
              type="button"
              onClick={() => setView('library')}
              className="btn-ghost text-xs"
            >
              全部资料
              <IconArrowUpRight size={13} />
            </button>
          }
        />

        {docsLoading ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="surface p-3.5">
                <Skeleton width="100%" height={40} />
                <Skeleton className="mt-3" width="72%" height={12} />
              </div>
            ))}
          </div>
        ) : documents.length > 0 ? (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {documents.map((doc, index) => (
              <DocTile key={doc.id} doc={doc} index={index} />
            ))}
          </div>
        ) : (
          <EmptyState
            icon={<IconFile size={20} />}
            title="还没有资料"
            description="传一份讲义、教材或笔记上来，我读完会把它拆成知识点，再带着你学。"
            action={
              <Button variant="primary" size="sm" icon={<IconLibrary size={14} />} onClick={() => setView('library')}>
                去上传
              </Button>
            }
          />
        )}
      </section>

      {/* ═══════════════════════════════════ 学习记录（只显示有内容时） */}
      {hasHistory && (
        <section className="space-y-3">
          <SectionTitle eyebrow="足迹" title="最近学过什么" />
          <Card padded={false} className="divide-y divide-line">
            {[
              ...due.slice(0, 3).map((item) => ({
                id: `due-${item.knowledge_point_id}`,
                title: item.title,
                text: `${masteryMark(item)} · ${lastTrouble(item)}`,
                when: item.due ? '该看了' : '已排期',
                icon: <IconClock size={14} />,
                tone: 'sienna' as const,
              })),
            ].map((row, index) => (
              <motion.div
                key={row.id}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.3, delay: index * 0.05 }}
                className="flex items-center gap-3 px-5 py-3"
              >
                <span className="shrink-0 text-ink-4">{row.icon}</span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-xs text-ink-1">{row.title}</p>
                  <p className="mt-0.5 text-2xs text-ink-3">{row.text}</p>
                </div>
                <span className="meta shrink-0">{row.when}</span>
              </motion.div>
            ))}
          </Card>
        </section>
      )}
    </div>
  )
}
