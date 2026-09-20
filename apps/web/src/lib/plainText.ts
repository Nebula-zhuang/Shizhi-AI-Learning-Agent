/**
 * 把模型输出的行内 Markdown 标记摘干净。
 *
 * ## 为什么这是共享工具而不是各页面各写一份
 *
 * 教学内容在本项目里**统一按纯文本渲染**（`<p>{line}</p>`），不引 Markdown 渲染器 ——
 * 引了就等于把"模型输出什么就渲染什么"的风险引进界面（注入、样式失控、
 * 一个没闭合的星号毁掉整段排版）。
 *
 * 于是出现同一个问题：模型很爱写 `**重点**`，而裸渲染出来就是
 * `**重点**` 连着星号一起显示给用户看。辅导页修过一次，
 * 自由学习页又遇到一次 —— **同一件事在第二个地方重写，第三处一定还会漏。**
 *
 * 所以抽到这里：谁是纯文本渲染，谁就调它。
 *
 * ## 只摘标记，不摘内容
 *
 * `` `Human` `` → `Human`（标识符本身保留）。
 * 目标是"去掉只对渲染器有意义的符号"，不是"把内容一起删掉"。
 */

/** `` `code` `` → `code` */
const INLINE_BACKTICK = /`([^`]*)`/g

/** `**粗体**` → `粗体` */
const INLINE_STRONG = /\*\*([^*]+)\*\*/g

/**
 * `*斜体*` → `斜体`
 *
 * ⚠️ 两侧都要求不是星号 —— 否则 `2*3*4` 这类算式会被误伤成斜体。
 * 教学内容里出现乘法是很常见的事。
 */
const INLINE_EMPHASIS = /(?<!\*)\*([^*\n]+)\*(?!\*)/g

export function stripInlineMarkup(text: string): string {
  return text
    .replace(INLINE_BACKTICK, '$1')
    .replace(INLINE_STRONG, '$1')
    .replace(INLINE_EMPHASIS, '$1')
}

/**
 * 按行清理整段文本。
 *
 * 比逐行调用 `stripInlineMarkup` 多做的事：把 `###` 这类行首标题标记也去掉。
 * 起因是模型偶尔无视提示词写 Markdown 标题 —— 裸渲染出来是一串井号。
 */
export function stripBlockMarkup(text: string): string {
  return text
    .split('\n')
    .map((line) => stripInlineMarkup(line).replace(/^\s{0,3}#{1,6}\s+/, ''))
    .join('\n')
}
