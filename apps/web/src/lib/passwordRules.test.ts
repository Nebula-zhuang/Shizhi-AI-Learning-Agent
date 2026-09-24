/**
 * 改密码的本地校验规则测试。
 *
 * ## 为什么值得单独测
 *
 * 这几条规则**必须和后端一致**（`apps/api/app/api/routes/auth.py`：
 * `len(new_password) < 6 or > 64 → 422`）。前端先拦一道的意义在于
 * **让用户按按钮之前就知道哪里不对** —— 如果规则和后端不一致，
 * 用户会看到"前端说没问题、后端却 422"这种最难解释的错误 ✗
 *
 * 所以这里既测规则本身，也**钉住与后端相同的边界**（5/6/64/65）。
 *
 * 运行：`npm run test:unit`
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { PASSWORD_MAX, PASSWORD_MIN, validatePassword, validatePasswordChange } from './passwordRules.ts'

// --------------------------------------------------------------------------- //
// 一、单个密码字段
// --------------------------------------------------------------------------- //
test('长度边界与后端一致（5 拒绝 / 6 通过 / 64 通过 / 65 拒绝）', () => {
  assert.ok(validatePassword('a'.repeat(PASSWORD_MIN - 1)), '5 位应当被拒')
  assert.equal(validatePassword('a'.repeat(PASSWORD_MIN)), null, '6 位应当通过')
  assert.equal(validatePassword('a'.repeat(PASSWORD_MAX)), null, '64 位应当通过')
  assert.ok(validatePassword('a'.repeat(PASSWORD_MAX + 1)), '65 位应当被拒')
})

test('空密码', () => {
  assert.equal(validatePassword(''), '请填写密码')
})

test('长度常量与后端口径相同', () => {
  assert.equal(PASSWORD_MIN, 6)
  assert.equal(PASSWORD_MAX, 64)
})

// --------------------------------------------------------------------------- //
// 二、整个表单
// --------------------------------------------------------------------------- //
const ok = { oldPassword: 'oldpass123', newPassword: 'newpass456', confirmPassword: 'newpass456' }

test('合法的表单没有提示', () => {
  assert.equal(validatePasswordChange(ok), null)
})

test('没填当前密码', () => {
  assert.equal(validatePasswordChange({ ...ok, oldPassword: '' }), '请填写当前密码')
})

test('新密码太短时，提示说的是"新密码"', () => {
  const problem = validatePasswordChange({ ...ok, newPassword: 'abc', confirmPassword: 'abc' })
  assert.ok(problem)
  assert.ok(problem.startsWith('新密码'), '别让用户以为是当前密码的问题')
})

test('两次不一致', () => {
  const problem = validatePasswordChange({ ...ok, confirmPassword: 'newpass457' })
  assert.equal(problem, '两次输入的新密码不一致')
})

test('⚠️ 新密码本身不合法时，先报长度而不是"两次不一致"', () => {
  // 两个一样的短密码 —— 用户两次填的明明一样，
  // 这时候说"不一致"是错的 ✗ 应当先说长度
  const problem = validatePasswordChange({
    oldPassword: 'oldpass123',
    newPassword: 'abc',
    confirmPassword: 'abc',
  })
  assert.ok(problem)
  assert.ok(!problem.includes('不一致'), '两次明明一样，不该说不一致')
  assert.ok(problem.includes('新密码'))
})

test('新密码与当前密码相同 → 拒绝', () => {
  const same = { oldPassword: 'samepass1', newPassword: 'samepass1', confirmPassword: 'samepass1' }
  assert.equal(validatePasswordChange(same), '新密码和当前密码一样，换了等于没换')
})

test('当前密码为空优先于其他提示', () => {
  const problem = validatePasswordChange({
    oldPassword: '',
    newPassword: 'abc',
    confirmPassword: 'xyz',
  })
  assert.equal(problem, '请填写当前密码')
})
