---
name: knowledge-verification
title: 可信度校验与联网核验
stage: P2
used_by: Workflow B / Tool: verify_knowledge、web_search、fetch_url
status: skeleton
---

# 可信度校验与联网核验

> 状态：**骨架**。P0 只固定结构；具体策略与提示词在 P2 阶段填充。
> 本文件与 `apps/api/app/agent/prompts/` 下的提示词模板一一对应。

## 用途

判定知识点是否可信，对可疑项主动联网查证并留存证据。

## 职责范围

- 三级校验策略：L1 规则 → L2 模型自评 → L3 联网核验
- 是否该联网的判定规则（避免对常识性知识浪费搜索配额）
- 核验检索式构造模板
- 四类裁决标准：confirmed / outdated / conflict / unverifiable
- 证据可信度分级：官方文档 > 权威教材 > 一般网页

## 输入 / 输出

待填充（P2）

## 策略与规则

待填充（P2）

## 提示词模板

待填充（P2），落地位置：`apps/api/app/agent/prompts/`

## 验收标准

待填充（P2）
