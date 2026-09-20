/**
 * 任务卡解析测试。
 *
 * ## 这个文件补的是一个真实事故
 *
 * 上线后用户反馈"生成内容重复"。查下来根因在这里：
 *
 * `parseTaskCard` 把引导语**拼接成一个字符串**存进 `lead`，
 * 而另一个函数 `uncoveredLines` 把它整串记成"已覆盖"，
 * **却拿单行去比对** —— 永远匹配不上，于是每一行都被判成"没覆盖"，
 * 在卡片下方又渲染了一遍。
 *
 * 更糟的是当时**这个模块一条测试都没有**，所以没人发现。
 * 现在的设计把 leftover 直接由解析函数产出（与分区同源），
 * 下面用"内容不重复"这组用例把这条不变量钉死。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseTaskCard, stripInlineMarkup } from './taskCard.ts'

/** 用户实际遇到的那条内容（原样保留反引号与中文引号） */
const REAL_CASE = `接口回调，就是把一个实现了接口的对象当作参数传进去，让方法在运行时调用它具体实现的方法。

**是什么？**
它本质是多态的应用：方法的参数是接口类型（比如\`Human\`），但实际传进来的是它的某个实现类对象（比如\`Chinese\`或\`American\`）；运行时才决定调用哪个类的\`sayHello()\`——这叫“运行时绑定具体实现”。

**为什么用？**
为了解耦和扩展：调用者（比如测试类）不依赖具体国籍类，只依赖\`Human\`接口；新增一个\`Japanese\`类也不用改测试代码，只要它实现了\`Human\`接口就行。这就是“解耦调用者与实现者”，也体现了“依赖倒置原则”。

**怎么用？**
关键三步：① 定义接口（如\`Human\`）；② 写多个实现类（如\`Chinese\`、\`American\`）；③ 方法接收该接口类型参数，在内部调用其方法（如\`human.sayHello()\`）。实验Part 3(1)(iv)正是用这种方式替代抽象类方案。

这道题问的是接口回调中“参数类型”和“实际调用对象”的关系。

要答到：
① 参数声明时写的是什么类型
② 实际传入的是什么类型的对象
③ 运行时调用的方法由谁决定

用一句话说明即可，不要展开。

那么：在接口回调中，方法参数声明为\`Human\`接口类型，实际传入\`Chinese\`对象，\`sayHello()\`方法最终由哪个类实现？参数类型、实参类型、方法归属三者分别是什么？`

/* ══════════════════════════════════════════════════════════════════════
   不变量：内容不许重复
   ══════════════════════════════════════════════════════════════════════ */

test('兜底行不吞掉已分区的行 —— 这是"内容重复"的直接原因', () => {
  const card = parseTaskCard(REAL_CASE)
  assert.ok(card, '应当能解析成任务卡')

  // 上一版这里会返回 8 行（全部 lead），导致卡片下方再渲染一遍
  assert.equal(card.leftover.length, 0, '已分区的行不该出现在兜底里')
})

test('引导语不会同时出现在 lead 与 leftover 里', () => {
  const card = parseTaskCard(REAL_CASE)!
  const leadText = card.leadLines.join('\n')

  for (const line of card.leftover) {
    assert.ok(
      !leadText.includes(line),
      `这一行同时出现在引导语和兜底里，会被渲染两遍：${line.slice(0, 30)}`,
    )
  }
})

test('要点不会同时出现在 points 与 leftover 里', () => {
  const card = parseTaskCard(REAL_CASE)!
  for (const point of card.points) {
    assert.ok(!card.leftover.some((l) => l.includes(point)), `要点被渲染两遍：${point}`)
  }
})

/* ══════════════════════════════════════════════════════════════════════
   「怎么答」的判定
   ══════════════════════════════════════════════════════════════════════ */

test('「怎么答」认出真正的回答方式', () => {
  const card = parseTaskCard(REAL_CASE)!
  assert.equal(card.howTo, '用一句话说明即可，不要展开。')
})

test('「怎么答」不许把长段解释当成回答方式', () => {
  const card = parseTaskCard(REAL_CASE)!

  // 上一版的真实 bug：这句里含"就行"，被误判成回答方式，
  // 于是"为什么用"的内容出现在了【怎么答】里 —— 看起来像同一段话出现两遍
  assert.ok(
    !card.howTo.includes('为了解耦和扩展'),
    '长段解释被误判成了回答方式',
  )
  assert.ok(
    card.leadLines.some((l) => l.includes('为了解耦和扩展')),
    '这段解释应当留在引导语里',
  )
})

test('回答方式排在要点之后也能被认出', () => {
  // 真实输出的顺序就是"先列要点，再说怎么答"。
  // 若加上"必须在要点之前"的位置判断，howTo 会永远是空的。
  const content = `铺垫一句。

要答到：
① 甲
② 乙

用两句话回答即可。

那么：这道题问的是什么？`
  const card = parseTaskCard(content)
  assert.ok(card)
  assert.equal(card.howTo, '用两句话回答即可。')
})

/* ══════════════════════════════════════════════════════════════════════
   行内标记清理
   ══════════════════════════════════════════════════════════════════════ */

test('反引号被摘掉，标识符本身保留', () => {
  const cleaned = stripInlineMarkup('参数是 `Human` 类型，方法是 `sayHello()`')
  assert.equal(cleaned, '参数是 Human 类型，方法是 sayHello()')
  assert.ok(!cleaned.includes('`'), '不该残留反引号')
})

test('星号强调被摘掉', () => {
  assert.equal(stripInlineMarkup('这是**重点**内容'), '这是重点内容')
})

test('乘号不被误伤', () => {
  // 单星强调的规则要求两侧都不是星号，避免把 2*3 当成斜体
  assert.equal(stripInlineMarkup('计算 2*3 的值'), '计算 2*3 的值')
})

test('问题里的反引号在解析阶段就被清掉', () => {
  const card = parseTaskCard(REAL_CASE)!
  assert.ok(!card.question.includes('`'), '问题里不该残留反引号')
  assert.ok(card.question.includes('Human'), '标识符本身要保留')
})

/* ══════════════════════════════════════════════════════════════════════
   容错：拆不动就退化成散文
   ══════════════════════════════════════════════════════════════════════ */

test('没有问句时返回 null（交给调用方渲染散文）', () => {
  assert.equal(parseTaskCard('这是一段普通讲解，没有提问。'), null)
})

test('空内容返回 null', () => {
  assert.equal(parseTaskCard(''), null)
})

test('只有一句问题也能成卡', () => {
  const card = parseTaskCard('那么：进程和线程的区别是什么？')
  assert.ok(card, '单句问题也是合法任务')
  assert.equal(card.points.length, 0)
  assert.equal(card.howTo, '')
  assert.ok(card.question.includes('进程和线程'))
})

test('问题之后的补充不会丢', () => {
  const card = parseTaskCard('铺垫。\n\n那么：这道题怎么答？\n\n答完我们再对一下。')
  assert.ok(card)
  assert.ok(card.tail.includes('对一下'), '问题之后的句子必须保留')
})

/* ══════════════════════════════════════════════════════════════════════
   「要答到三点：」这种区段标签
   ══════════════════════════════════════════════════════════════════════ */

test('「要答到三点：」不会被当成第一个要点', () => {
  // 真实输出的原样：模型用一句标签给下面的清单起头
  const content = `铺垫一句。

要答到三点：
① 它必须概括几个总体趋势（填数字）
② 它能不能出现具体数据或年份（二选一：能 / 不能）
③ 它要用什么类型的连词来连接趋势

用最简短的方式回答，不要解释。

那么：Overview段必须概括几个总体趋势？`
  const card = parseTaskCard(content)
  assert.ok(card)

  assert.equal(card.points.length, 3, '应当只有 3 个要点，标签不算')
  assert.ok(
    !card.points.some((p) => p.includes('三点')),
    '「三点：」被误当成了要点，真实要点会被挤成 2/3/4',
  )
  assert.ok(card.points[0].includes('总体趋势'), '第 1 个要点应当是真正的那条')
})

test('各种区段标签写法都不算要点', () => {
  for (const label of ['要答到三点：', '以下三点：', '答到 3 点：', '要点如下：']) {
    const content = `${label}\n① 甲\n② 乙\n\n那么：问题是什么？`
    const card = parseTaskCard(content)
    assert.ok(card, `${label} 应当能解析`)
    assert.equal(card.points.length, 2, `${label} 的标签不该计入要点`)
  }
})

/* ══════════════════════════════════════════════════════════════════════
   更多「怎么答」的措辞
   ══════════════════════════════════════════════════════════════════════ */

test('「用最简短的方式回答，不要解释」被认成回答方式', () => {
  const content = `铺垫。

要答到：
① 甲
② 乙

用最简短的方式回答，不要解释。

那么：问题是什么？`
  const card = parseTaskCard(content)
  assert.ok(card)
  assert.equal(card.howTo, '用最简短的方式回答，不要解释。')
})

/* ══════════════════════════════════════════════════════════════════════
   位置：回答方式在要点之后
   ══════════════════════════════════════════════════════════════════════ */

test('铺垫里描述字数的句子不会被当成回答方式', () => {
  // 真实误判：这句短，且含"用 1–2 句话"，被 how-to 规则误伤，
  // 结果整句铺垫跑到了【怎么答】里
  const content = `图表作文的Overview段落，是用1–2句话高度凝练图表中最突出的动态特征的独立段落。

这道题检验你是否理解它的核心要求。

要答到三点：
① 它应概括几个总体趋势
② 它能否出现具体数字或年份
③ 它要用什么类型的连词

用最简短的方式回答。

那么：Overview段的核心要求是什么？`
  const card = parseTaskCard(content)
  assert.ok(card)

  assert.equal(card.howTo, '用最简短的方式回答。', '怎么答应当是最后那句贴士')
  assert.ok(
    card.leadLines.some((l) => l.includes('高度凝练')),
    '描述字数的铺垫句应当留在引导语里',
  )
})

test('要点之后的那句才是回答方式', () => {
  const content = `铺垫。

要答到：
① 甲
② 乙

用两句话分别回答，每句只说一个点。

那么：问题是什么？`
  const card = parseTaskCard(content)
  assert.ok(card)
  assert.equal(card.howTo, '用两句话分别回答，每句只说一个点。')
})

test('没有要点区段时不设位置限制', () => {
  // 只有一句回答方式、没有要点清单时，它照样要能被认出来
  const card = parseTaskCard('这是铺垫。\n\n用一句话回答即可。\n\n那么：问题是什么？')
  assert.ok(card)
  assert.equal(card.howTo, '用一句话回答即可。')
})

test('几种常见回答方式措辞都能认出', () => {
  const cases = [
    '用一句话说明即可。',
    '用两句话回答即可。',
    '请比较 A 和 B。',
    '请给出一个例子。',
    '最简短地回答。',
    '不必展开。',
    '用最简短的方式回答，不要解释。',
  ]
  for (const howTo of cases) {
    const card = parseTaskCard(`铺垫。\n\n${howTo}\n\n那么：问题是什么？`)
    assert.ok(card)
    assert.equal(card.howTo, howTo, `没能认出「${howTo}」`)
  }
})
