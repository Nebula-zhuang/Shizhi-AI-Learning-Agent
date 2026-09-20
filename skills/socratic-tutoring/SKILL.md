---
name: socratic-tutoring
title: 苏格拉底式教学策略
stage: P4
used_by: Tutor Agent Runtime（项目核心 Skill）
status: skeleton
---

# 苏格拉底式教学策略

> 状态：**骨架**。P0 只固定结构；具体策略与提示词在 P4 阶段填充。
> 本文件与 `apps/api/app/agent/prompts/` 下的提示词模板一一对应。

## 用途

定义 Agent 的六种教学动作及其触发条件，以及多种讲解风格。

## 职责范围

- 六种教学动作定义：追问 / 讲解 / 换讲法 / 升难度 / 降难度 / 总结
- 各动作的触发阈值，须与 apps/api/app/agent/policy.py 的硬约束保持一致
- 讲解风格库：类比、公式推导、生活例子、图示描述
- 提问设计法：由浅入深的追问链
- 引用优先级的表述规范：用户资料 > 联网证据 > 模型通识

## 输入 / 输出

待填充（P4）

## 策略与规则

待填充（P4）

## 提示词模板

待填充（P4），落地位置：`apps/api/app/agent/prompts/`

## 验收标准

待填充（P4）
