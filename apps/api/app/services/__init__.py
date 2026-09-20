"""业务逻辑层（P1 已实现文档与知识点服务）。

Agent 只负责决策，具体业务实现放在这里，便于单测与替换。

  document_service.py  - 文档增删查改与状态机
  knowledge_service.py - 知识点抽取（批次组装 / 校验 / 回填页码 / 去重 / 质量闸门）
  ingest_pipeline.py   - 摄取流水线编排
  ingest_runner.py     - 进程内轻量任务执行器（非消息队列）

抽取规则见 skills/knowledge-extraction/SKILL.md。
"""
