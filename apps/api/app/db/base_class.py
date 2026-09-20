"""SQLAlchemy 声明式基类（不含任何模型导入）。

为什么不把 Base 直接定义在 app/db/base.py：
    注册模型需要一个「导入全部模型」的聚合文件，而模型文件又要导入 Base。
    如果 Base 与被聚合的导入放在同一个模块里，就会形成循环导入 ——
    当某个 model 模块被**首先**导入时（例如脚本里直接
    `from app.models.xxx import Xxx`），Base 所在模块尚未完成初始化，直接报错。

    因此这里拆成两层：
        base_class.py  ← 只定义 Base，无任何依赖（本文件）
        base.py        ← 导入 Base 后再导入全部模型，供 Alembic 使用

    拆分后，模型模块只依赖 base_class，不存在任何环。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """项目统一基类。

    约定：
    - 表名用复数小写下划线（users / knowledge_points）
    - 主键统一 BIGINT 自增
    - 时间字段统一 created_at / updated_at，由数据库侧默认值兜底
    """

    def to_dict(self) -> dict:
        """轻量序列化，便于调试与日志。"""
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
