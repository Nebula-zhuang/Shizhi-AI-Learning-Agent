"""SQLAlchemy ORM 模型。

  app_meta.py        - 基础设施表（P0）
  conversation.py    - 自由学习对话与消息（P7）
  document.py        - 上传的资料（P1）
  chunk.py           - 带页码的语义块（P1）
  knowledge_point.py - 结构化知识点（P1）
  user.py            - 用户账号（P6）

注意：Base 定义在 app/db/base_class.py，模型文件一律从那里导入；
      db/base.py 只是供 Alembic 使用的聚合入口，从它导入会触发循环依赖。
"""
