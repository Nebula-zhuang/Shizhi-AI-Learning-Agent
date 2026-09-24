/**
 * 改密码时的本地校验规则。**零依赖** —— 前端单测可以直接引用它。
 *
 * ## 为什么单独一个文件
 *
 * 规则本身很短（非空 / 长度 / 两次一致），但它有两条硬要求：
 *
 * 1. **必须和后端一致**。后端 `POST /api/auth/password` 的规则是
 *    `len(new_password) < 6 or > 64 → 422`（见 `apps/api/app/api/routes/auth.py`）✓
 *    前端先拦一道不是为了替代后端，是让用户**按按钮之前**就知道哪里不对 ✓
 * 2. **不能有别的依赖**。放在 `api/auth.ts` 里会带上 `./http` 的运行时导入 ✗
 *    而单测跑 `node --experimental-strip-types` 会真的去解析模块路径 →
 *    引用它的测试必然 `ERR_MODULE_NOT_FOUND` ✗（这是 5C-1 踩过的坑 ✓）
 *
 * 所以规则放这里，`api/auth.ts` 的 `validatePassword` 与它保持同一口径 ✓
 */

/** 密码长度上下限。**与后端同一口径** —— 改这里必须同时改后端。 */
export const PASSWORD_MIN = 6
export const PASSWORD_MAX = 64

/** 单个密码字段的校验。返回 `null` 表示没问题。 */
export function validatePassword(value: string): string | null {
  if (!value) return '请填写密码'
  if (value.length < PASSWORD_MIN) return `至少 ${PASSWORD_MIN} 位`
  if (value.length > PASSWORD_MAX) return `不超过 ${PASSWORD_MAX} 位`
  return null
}

/**
 * 改密码表单的整体校验。返回 `null` 表示可以提交。
 *
 * ⚠️ 顺序有意为之：**先看新密码本身合不合法，再看两次是否一致**。
 * 反过来（先比一致）的话，用户把两个一样的短密码填进去，
 * 会先被告知"两次不一致"—— 但他两次明明填的一样 ✗ 那种提示是错的。
 */
export function validatePasswordChange(input: {
  oldPassword: string
  newPassword: string
  confirmPassword: string
}): string | null {
  if (!input.oldPassword) return '请填写当前密码'

  const problem = validatePassword(input.newPassword)
  if (problem) return `新密码${problem}`

  if (input.newPassword !== input.confirmPassword) return '两次输入的新密码不一致'

  if (input.newPassword === input.oldPassword) return '新密码和当前密码一样，换了等于没换'
  return null
}
