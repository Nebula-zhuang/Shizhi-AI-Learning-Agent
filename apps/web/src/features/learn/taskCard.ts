/**
 * 教学任务卡的结构化解析。
 *
 * ## 为什么要解析，以及为什么必须"容错"
 *
 * 后端提示词要求助教提问时写清三件事：
 *
 *     这道题问的是：……
 *     要答到：
 *     ① ……
 *     ② ……
 *     用两句话以内说清楚就行 —— ……
 *     那么：……？
 *
 * 直接当一段散文渲染也能读，但学习者最需要的是"**我要回答什么**"——
 * 把它拆成任务卡（引导语 / 问题 / 要答到的几点 / 回答方式）会让这一步不需要寻找。
 *
 * 风险在于：这是**模型输出**，不是结构化字段。模型会换措辞、漏标号、
 * 甚至把同一段话写成两遍。所以策略是 —— **能拆就拆，拆不动就当散文**：
 *
 *   · 找不到明确的问题句 → 整段返回 `null`，调用方渲染成普通正文
 *   · 找到问题但没有要点列表 → 仍然产出任务卡，只是没有「要答到」区块
 *   · 任何一步失败都不抛异常
 *
 * 也就是说：**这个模块最差的表现是"退化成原文"，而不是"显示错东西"。**
 */

export interface TaskCard {
  /** 引导语：这道题在问什么、为什么要问，以及必要的铺垫讲解 */
  lead: string
  /**
   * 引导语的**逐行**形式。
   *
   * 为什么留一份行数组而不是只留拼接后的字符串：行是"渲染单位"，
   * 拼接串是"提示词单位"。以前只存拼接串，然后在另一个函数里拿单行去和它比对，
   * 结果**每一行都判成没覆盖、又渲染了一遍** —— 那就是"内容重复"的来源。
   * 现在行数组与 `lead` 同源产出，不存在两边对不上的可能。
   */
  leadLines: string[]
  /** 必须覆盖的要点，已去掉编号 */
  points: string[]
  /** 回答方式提示（"用两句话以内"这类） */
  howTo: string
  /** 要回答的那个问题本身 */
  question: string
  /** 问题句之后的补充（少见，但真出现时不能丢） */
  tail: string
  /**
   * 未被上面任何区块接住的行。
   *
   * **由解析函数自己产出**，不再让调用方另算一遍 ——
   * "解析"和"算覆盖"分成两个函数，就是上一版分歧的根源。
   */
  leftover: string[]
}

/** 圈码 ①–⑳ 与阿拉伯编号 "1." "1、" "(1)" */
const MARKER = /^\s*(?:[①-⑳]|[(（]?\d{1,2}[)）.、])\s*/

/** 指向"最终问题"的行首标记 */
const QUESTION_LEAD = /(那么|请问|所以|现在请|请回答)\s*[:：]?/

/**
 * 指向"回答方式"的区段标记。
 *
 * ⚠️ **不能只靠"就行 / 即可"这类词判定。**
 * 上一版就是被这个坑了：「只要它实现了 Human 接口**就行**」是一句普通讲解，
 * 却被判成"回答方式"，于是那段"为什么用"的内容被塞进了【怎么答】里 ——
 * 看起来就像同一段话出现了两遍。
 *
 * 现在的规则是**同时**满足两个条件：
 *   1. 命中"回答形式"的语汇（句话 / 字 / 词 / 行 / 作答 / 不要展开 …）
 *   2. 整行**够短**（回答方式是贴士，不会是一整段解释）
 * 长段落一律不当回答方式。
 */
const HOW_TO_HINT =
  /(用.{0,8}(句话|个字|个词|行)|一句话|作答|回答方式|填空形式|请比较|请判断|请给出|(不要|不必|无需|别)(解释|展开|赘述)|(最)?简短|即可|就行)/

/**: 回答方式的行长上限。超过这个长度几乎一定是在讲解，不是在说怎么答。 */
const HOW_TO_MAX_CHARS = 44

/** 指向"要答到"的区段标记 */
const POINTS_LEAD = /要答到|需要答到|必须答到|要点如下|回答要点/

/**
 * 判断"要答到"后面跟的是**区段标题**而不是第一个要点。
 *
 * 模型常写成「要答到三点：」「以下三点：」「要点如下 3 条」——
 * 这些是**给下面清单起头的标签**，不是要点本身。
 * 上一版没做这个区分，于是「三点：」被当成第 1 个要点，
 * 后面三条真实要点被挤成 2/3/4，用户看到的编号是错的。
 */
const POINT_LABEL_ONLY = /^[一二三四五六七八九十两0-9]+\s*[点条个项]?\s*[:：]?$/

function isPointLabel(text: string): boolean {
  const trimmed = text.trim()
  if (!trimmed) return true
  // 以冒号结尾的基本都是标签（"三点：" / "需答到以下几点："）
  if (/[:：]$/.test(trimmed)) return true
  return POINT_LABEL_ONLY.test(trimmed)
}

/**
 * 行内标记清理。
 *
 * **实现已移到 `src/lib/plainText.ts`** —— 自由学习页也遇到了同样的问题
 * （模型写 `**重点**`，裸渲染出来连着星号一起显示），
 * 同一件事在第二处重写，第三处一定会漏。所以抽成共享工具。
 *
 * 这里保留再导出是为了不打断既有调用与测试。
 */
// ⚠️ 带 .ts 扩展名：这个模块会被 Node 的测试加载器直接跑，
// 而它的 ESM 解析要求显式扩展名（Vite/tsc 都允许，tsconfig 已开 allowImportingTsExtensions）。
import { stripInlineMarkup } from '../../lib/plainText.ts'

export { stripInlineMarkup }

function lines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line, index, all) => !(line === '' && all[index - 1] === ''))
}

/**
 * 从助教的一段正文里抽出任务卡。
 *
 * 返回 `null` 表示"这段不是一道可以结构化的问题"，调用方按普通正文渲染。
 */
export function parseTaskCard(content: string): TaskCard | null {
  try {
    const rows = lines(content)
    if (rows.length === 0) return null

    // ── 1) 找问题句：优先取带"那么："的那一行，其次取最后一个问号结尾的行
    let questionIndex = -1
    for (let i = rows.length - 1; i >= 0; i--) {
      if (QUESTION_LEAD.test(rows[i])) {
        questionIndex = i
        break
      }
    }
    if (questionIndex < 0) {
      for (let i = rows.length - 1; i >= 0; i--) {
        if (/[？?]\s*$/.test(rows[i])) {
          questionIndex = i
          break
        }
      }
    }
    if (questionIndex < 0) return null

    const rawQuestion = stripInlineMarkup(
      rows[questionIndex].replace(QUESTION_LEAD, '').trim(),
    )
    if (rawQuestion.length < 4) return null

    // 问题句之后的补充。后端提示词要求问题放最后，所以这里通常是空的；
    // 真出现内容时留着，避免丢句子。
    const tail = rows
      .slice(questionIndex + 1)
      .filter((row) => row.length > 0)
      .join('\n')
      .trim()

    // ── 2) 问题之前的行：分流成「要点 / 回答方式 / 引导语」，并记录谁被接住了
    const consumed = new Set<number>()
    consumed.add(questionIndex)
    for (let i = questionIndex + 1; i < rows.length; i++) consumed.add(i)

    // 要点区段的起点。回答方式**只在它之后**才算数 —— 见下方注释。
    const pointsStart = rows.findIndex(
      (row, index) =>
        index < questionIndex && (POINTS_LEAD.test(row) || MARKER.test(row)),
    )

    const leadLines: string[] = []
    const points: string[] = []
    const howToLines: string[] = []

    for (let i = 0; i < questionIndex; i++) {
      const row = rows[i]

      if (POINTS_LEAD.test(row)) {
        consumed.add(i)
        // "要答到：" 后面可能直接跟内容（同一行）。
        // 但如果跟的只是「三点：」这种**区段标签**，就不能当成要点 ——
        // 否则真实要点会被挤成 2/3/4，编号就错了。
        const inline = row.replace(POINTS_LEAD, '').replace(/^[:：]\s*/, '').trim()
        if (inline && !isPointLabel(inline)) points.push(stripInlineMarkup(inline))
        continue
      }

      if (MARKER.test(row)) {
        // 带编号的行基本都是要点。即便没出现过"要答到"，编号本身也是强信号
        points.push(stripInlineMarkup(row.replace(MARKER, '').trim()))
        consumed.add(i)
        continue
      }

      // 回答方式：**短** + 命中语汇 + **排在要点之后**。
      //
      // 三条缺一不可，每一条都是被真实误判逼出来的：
      //
      //   · 只看语汇 → 「只要实现了 Human 接口**就行**」这种长解释被误判
      //   · 再加长度 → 「Overview 段是用 1–2 句**话**凝练…」这种**在讲字数要求**的
      //     描述句又被误判（它短，且含"用…句话"）
      //   · 加上位置 → 才对。真实的输出结构是
      //     `铺垫 → 要答到 → 要点清单 → 怎么答 → 问题`，
      //     **回答方式永远在要点之后**；而上面那句描述属于铺垫，排在要点之前。
      //
      // 没有要点区段时（例如只有一句"用一句话回答即可。"）不设位置限制。
      const afterPoints = pointsStart < 0 || i > pointsStart
      if (afterPoints && row.length <= HOW_TO_MAX_CHARS && HOW_TO_HINT.test(row)) {
        howToLines.push(stripInlineMarkup(row))
        consumed.add(i)
        continue
      }

      // 其余都是引导语（必要的铺垫讲解）
      leadLines.push(stripInlineMarkup(row))
      consumed.add(i)
    }

    // ── 3) 没被接住的行。正常情况下为空；有的话原样交给调用方，不能静默丢句
    const leftover = rows.filter((_, index) => !consumed.has(index))

    const lead = leadLines.join('\n').trim()
    const howTo = howToLines.join(' ').trim()

    return {
      lead,
      leadLines,
      points,
      howTo,
      question: rawQuestion,
      tail,
      leftover,
    }
  } catch {
    // 解析永不让界面崩掉。宁可退回散文
    return null
  }
}

/**
 * 保留此导出仅为兼容既有测试与调用方。
 *
 * **新代码请直接用 `card.leftover`** —— 那是解析函数自己算出来的，
 * 与分区逻辑同源，不可能对不上。这个函数只能靠"再算一遍"来猜，
 * 而上一版正是猜错了才导致内容重复。
 *
 * @deprecated 用 `card.leftover`
 */
export function uncoveredLines(_content: string, card: TaskCard): string[] {
  return card.leftover
}
