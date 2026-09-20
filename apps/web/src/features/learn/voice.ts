/**
 * 语言层 —— 把系统的状态翻译成学习者能听懂的话。
 *
 * 这是这次重构最关键的一层。原界面把**后端字段直接端给了用户**：
 *
 *   掌握度 0.00 · 作答 3 次 · 连错 3 · 判定 weak · 相似度 0.34 · 置信度 95% · 孤立节点 2
 *
 * 这些话对学习者是噪音。这一层负责两件事：
 *
 * 1. **翻译**：把状态说成助教会说的那句话（"你上次在这儿卡住了"）。
 * 2. **守一条纪律**：下面这些函数里**不允许出现**英文术语、数据库字段名、
 *    状态机名字、置信度/相似度这类技术指标。它们只能在 `why/`（为什么这样教）
 *    和 `dev/`（开发者面板）里出现。
 *
 * 规则：凡是用户主界面要显示的文字，**先过这一层**；不能直接把后端字段拼进 UI。
 */

import type { TutorAssessment, TutorLearnerState, WeakPoint } from '../../api/tutor'

// --------------------------------------------------------------------------- #
// 掌握度
// --------------------------------------------------------------------------- #

/** 把 0~1 的掌握度说成一句话。刻意不给小数点后的精确值 —— 那不是给人看的。 */
export function masteryWords(mastery: number): string {
  if (mastery >= 0.85) return '基本拿下了'
  if (mastery >= 0.7) return '快了，再稳一稳'
  if (mastery >= 0.5) return '明白一半了'
  if (mastery >= 0.3) return '刚有点感觉'
  if (mastery > 0) return '还不熟'
  return '还没开始'
}

/**
 * 把知识点的处境说全。
 *
 * 掌握度 0 有两种完全不同的处境：从没学过，和学了但一次没答对。
 * 对后者说"还没开始"，学习者会觉得系统没在看他 —— 所以分开说。
 */
export function pointStatusWords(state: Pick<TutorLearnerState, 'mastery' | 'attempt_count'>): string {
  if (state.attempt_count === 0) return '还没开始'
  if (state.mastery <= 0) return '还没答到点上'
  return masteryWords(state.mastery)
}

/**
 * 待复习/薄弱点列表里的一行掌握程度。
 *
 * **直接复用 `pointStatusWords`，不要另写一套判断。**
 * 之前这里单独实现了"没作答过才叫未开始"，结果和正文的判断不一致 ——
 * 同一个知识点在列表里说"还没开始"、在详情里说"还没答到点上"；
 * 更糟的是"连错 3 次"和"还没开始"会并列出现。现在只有一处判断，两处显示天然一致。
 */
export function masteryMark(item: Pick<WeakPoint, 'attempt_count' | 'mastery'>): string {
  return pointStatusWords(item)
}

// --------------------------------------------------------------------------- #
// 上次卡在哪
// --------------------------------------------------------------------------- #

const ERROR_WORDS: Record<string, string> = {
  concept_confusion: '概念混淆',
  memory_gap: '记不牢',
  reasoning_break: '推理没跟上',
  misread: '看错题意',
  none: '没答到点上',
}

/** "上次错在哪"的一句话 */
export function lastTrouble(item: Pick<WeakPoint, 'attempt_count' | 'consecutive_wrong' | 'last_error_type'>): string {
  const error = item.last_error_type ? ERROR_WORDS[item.last_error_type] ?? '没答到点上' : '没答到点上'
  if (item.consecutive_wrong >= 2) return `连错 ${item.consecutive_wrong} 次，卡在${error}`
  if (item.attempt_count > 0) return `试过 ${item.attempt_count} 次，问题出在${error}`
  return '还没试过'
}

/**
 * 把解析警告翻译成学习者能看懂的话。
 *
 * 后端的警告是写给开发者看的，实测原文长这样：
 *
 *   「已提取 9 张图片；公式与图表的内容理解需要多模态模型，**P1 暂不支持**。」
 *   「第 11/26 批产出 9 条知识点，超过 8 条，粒度可能过细。」
 *   「第 13/26 批未产出知识点。」
 *
 * 里面有**内部阶段名（P1）**、**批次号（13/26）**、**内部阈值（超过 8 条）** ——
 * 这些对学习者毫无意义。这里做三件事：
 *   1. 有意义的（图片读不了）翻成人话，并保留数量
 *   2. 纯内部信号（批次粒度、空批）直接**丢掉** —— 它们不影响学习者任何决策
 *   3. 不认识的兜底成一句老实话，不假装没事
 *
 * 不改后端文案（那是开发者的排障依据），只在前端换一种说法。
 */
export function friendlyWarnings(
  warnings: { code?: string; message: string; page_no?: number | null }[],
): string[] {
  const out: string[] = []
  let imageCount = 0
  let unknown = 0

  for (const warning of warnings) {
    const code = warning.code ?? ''
    const message = warning.message ?? ''

    if (code === 'FORMULA_NOT_EXTRACTED') {
      // 从原文里抠出图片数量，抠不到就笼统说
      const matched = /(\d+)\s*张图片/.exec(message)
      imageCount += matched ? Number(matched[1]) : 0
      continue
    }

    // 批次粒度、空批这类是给我们自己调抽取参数用的，不该出现在用户面前
    if (code === 'EMPTY_BATCH' || code === 'TOO_FINE_GRAINED') continue

    unknown += 1
  }

  if (imageCount > 0) {
    out.push(
      `资料里有 ${imageCount} 张图片（比如图表和公式），图片里的内容我暂时读不了，` +
        `讲解时会以文字内容为准。`,
    )
  }
  if (unknown > 0) {
    out.push(`另外有 ${unknown} 处我没能完全读懂，讲到这里时可能讲得少一点。`)
  }
  return out
}

// --------------------------------------------------------------------------- #
// 回顾语（跨会话记忆的一句话）
// --------------------------------------------------------------------------- #

/** `turn.memory.knowledge_state` 里我们真正会用到的字段 */
interface RecallState {
  mastery?: unknown
  attempt_count?: unknown
  consecutive_wrong?: unknown
  last_error_type?: unknown
}

/**
 * 组织"我还记得你上次……"这句话。
 *
 * **为什么不直接用后端给的 `recall_note`**：那是给开发者看的口吻，实测原文是
 *
 *   「你上次学过这个知识点，作答 3 次，**掌握度 0.00**。结束时连续答错 3 次，
 *     卡在 **记忆缺口** 上。……」
 *
 * `掌握度 0.00` 是数据库的说法，学习者既看不懂也不该被要求看懂。
 * 所以在前端**用同一批真实记录重新组织句子** —— 事实来自数据库（不会被编造），
 * 只是换一种说法。后端文案不动（那是排障依据）。
 *
 * 拿不到结构化状态时退回后端原文，保证不会"什么都不说"。
 */
export function recallSentence(memory: {
  knowledge_state?: Record<string, unknown> | null
  is_due?: boolean
  recall_note?: string
}): string {
  const state = memory.knowledge_state as RecallState | null | undefined
  if (!state) return memory.recall_note ?? ''

  const mastery = Number(state.mastery ?? 0)
  const attempts = Number(state.attempt_count ?? 0)
  const wrongStreak = Number(state.consecutive_wrong ?? 0)
  const errorType = typeof state.last_error_type === 'string' ? state.last_error_type : null
  const errorWord = errorType ? ERROR_WORDS[errorType] ?? '没答到点上' : null

  let head: string
  if (wrongStreak >= 2) {
    head = `你上次在这个知识点上连错 ${wrongStreak} 次${errorWord ? `，卡在${errorWord}上` : ''}。`
  } else if (attempts > 0 && errorWord) {
    head = `你上次试过这个知识点，问题出在${errorWord}上。`
  } else if (attempts > 0) {
    head = `你上次学过这个知识点，${masteryWords(mastery)}。`
  } else {
    head = '这个知识点你还没碰过。'
  }

  const due = memory.is_due ? '按复习计划，现在正好该回顾了。' : ''
  return `${head}${due}我们先把上次没通的地方捡起来，再往下走。`
}

// --------------------------------------------------------------------------- #
// 评估反馈
// --------------------------------------------------------------------------- #

export function feedbackHeadline(assessment: TutorAssessment): string {
  return assessment.correct ? '这次答对了' : '这次没答对'
}

/** 把评估等级说成话，不报分数 */
export function assessmentWords(assessment: TutorAssessment): string {
  switch (assessment.level) {
    case 'mastered':
      return '答得完整'
    case 'vague':
      return '答得不够透'
    case 'not_mastered':
      return '没答到点上'
    default:
      return assessment.correct ? '答对了' : '没答对'
  }
}

// --------------------------------------------------------------------------- #
// 「下一步为什么这样教」
// --------------------------------------------------------------------------- #

/**
 * 把"接下来要做什么、为什么"说给学习者听。
 *
 * **为什么不直接用后端的 `turn.reason`**：那句话是写给**系统**看的，实测原文长这样：
 *
 *   「学习者刚答对且理解标准差与波动性的关系，需 **probing** 确认是否能迁移至
 *     偏度/峰度等其他统计量的解释。」
 *
 * 两个问题：用第三人称谈论学习者（"学习者刚答对"），而且泄漏了内部动作名（probing）。
 * 端给学习者，感觉像"系统在背后议论我"。
 *
 * 所以在前端**按命中的规则自己组一句话**。规则名（R1–R8）与动作名都是稳定字段，
 * 比解析一句自然语言稳健得多，也不动后端的理由文本（那是排障依据）。
 */
export function nextStepWords(rule: string | null | undefined, action: string): string {
  // 先看命中的规则 —— 规则代表"状态已经走到这一步了"，比动作本身更能说明原因
  switch (rule) {
    case 'R1_mastered':
      return '这块你已经稳住了，我把要点收一收，这次就学到这里。'
    case 'R2_wrong_streak_3':
      return '连着卡了几次，我们退一步，先把最基础的那一步垫实。'
    case 'R3_wrong_streak_2':
      return '刚才那种讲法没通，我换个角度再讲一遍。'
    case 'R4_shallow_correct':
      return '方向对了，但说得还不够透，我追问一句看看。'
    case 'R5_correct_streak_2':
      return '你连着答对了几次，我把难度提一档，看看能不能稳住。'
    case 'R6_wrong_and_weak':
      return '这里基础还比较薄，我先把它讲清楚再往下走。'
    case 'R7_first_contact':
      return '这个是头一回接触，我先讲清楚，再问你一句确认一下。'
    default:
      break
  }

  // 自由区（R8_default 或没有规则信息）：退回到按动作说一句通用的话
  switch (action) {
    case 'probe':
      return '方向对了，我再追问一句，看看是不是真懂了。'
    case 'explain':
      return '这个概念还没稳，我先把它讲清楚。'
    case 'harder':
      return '你答得不错，我往深里再推一步。'
    case 'easier':
      return '换个更小的口子，我们从最基础的那一步重新切进来。'
    case 'rephrase':
      return '换个讲法，再来一遍。'
    case 'summarize':
      return '这块可以收口了，我把要点串一下。'
    default:
      return ''
  }
}

// --------------------------------------------------------------------------- #
// 时间与日期
// --------------------------------------------------------------------------- #

/** 一天里的问候。用客户端时钟，不编造数据。 */
export function greeting(): string {
  const hour = new Date().getHours()
  if (hour < 5) return '夜深了'
  if (hour < 11) return '早上好'
  if (hour < 14) return '中午好'
  if (hour < 18) return '下午好'
  return '晚上好'
}

/** 相对时间："2 小时前" */
export function agoText(iso: string | null | undefined): string {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  if (diff < 0) return ''
  const minutes = Math.floor(diff / 60000)
  if (minutes < 60) return `${Math.max(1, minutes)} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.floor(hours / 24)} 天前`
}

/** 秒数 → 好读的中文间隔 */
export function humanizeSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} 小时`
  return `${Math.round(seconds / 86400)} 天`
}

// --------------------------------------------------------------------------- #
// 学习情况概览
// --------------------------------------------------------------------------- #

export interface OverviewLike {
  mastered: number
  learning: number
  weak: number
  untouched: number
  tracked: number
  knowledge_point_total: number
}

/**
 * 用一句话概括学习进度。刻意先讲"拿下了多少"，再说"还差多少" ——
 * 顺序会影响学习者怎么看待自己的进度。
 */
export function progressSentence(overview: OverviewLike): string {
  if (overview.tracked === 0) return '还没有开始'
  const parts: string[] = []
  if (overview.mastered > 0) parts.push(`已经拿下 ${overview.mastered} 个`)
  if (overview.learning > 0) parts.push(`正在学 ${overview.learning} 个`)
  if (overview.weak > 0) parts.push(`要补 ${overview.weak} 个`)
  const done = parts.join('，')
  if (!done) return `你试过 ${overview.tracked} 个知识点`
  return `${done}；一共 ${overview.knowledge_point_total} 个知识点`
}

// --------------------------------------------------------------------------- #
// 错误与提示（把技术错误说成人话）
// --------------------------------------------------------------------------- #

/**
 * 把后端的报错翻译成学习者能看懂的话。
 *
 * 后端报错是给开发者看的（"HTTP 409"、"fetch failed"、"422 Unprocessable Entity"），
 * 端给用户只会让人慌。这里只保留"出了什么事 + 我能怎么办"。
 * 不认识的错误给一个**老实**的兜底，而不是装作没事。
 */
export function friendlyError(raw: string): string {
  const text = raw.toLowerCase()

  if (/(failed to fetch|networkerror|fetch failed|connection refused|econnrefused)/.test(text)) {
    return '连不上服务。请确认学习伙伴还在运行，稍后再试。'
  }
  if (/timeout|timed out/.test(text)) {
    return '等得有点久，服务可能正在忙。稍后再试一次。'
  }
  if (/401|unauthorized|api key|invalid.*key/.test(text)) {
    return '凭据好像有问题，请联系维护的人检查配置。'
  }
  if (/402|insufficient|balance|余额/.test(text)) {
    return '模型额度暂时不够了，请联系维护的人。'
  }
  if (/404|not found/.test(text)) {
    return '没找到要用的东西，可能已经被删掉了。'
  }
  if (/409|conflict|状态冲突/.test(text)) {
    return '这一步和之前的状态对不上，刷新一下再试。'
  }
  if (/422|validation|unprocessable/.test(text)) {
    return '内容没通过检查，请换个说法再提交。'
  }
  if (/500|internal/.test(text)) {
    return '服务内部出了点问题，稍后再试。'
  }
  // 不认识的老实说，别装作没事
  return `出了点问题，没能完成这一步。${raw ? `（${raw.slice(0, 80)}）` : ''}`
}
