"""Agent 运行时层（项目核心）。P0 仅保留目录结构，不实现逻辑。

规划内容：
  runtime.py  —— Agent Loop：观察 → 决策 → 调用工具 → 再决策
  policy.py   —— 教学动作状态机与阈值硬约束
  tools/      —— 每个 Tool 一个文件 + registry.json schema 注册表
  workflows/  —— 确定性流水线：ingest（资料摄取）、verify（可信度校验）
  prompts/    —— 各 Skill 的提示词模板
"""
