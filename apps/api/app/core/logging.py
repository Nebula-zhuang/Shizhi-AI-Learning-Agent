"""统一日志配置。

避免在每个模块里 print / basicConfig。全项目统一通过 get_logger() 获取 logger。
"""

from __future__ import annotations

import logging
import sys

from app.core.config import settings

_CONFIGURED = False

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
_DATEFMT = "%H:%M:%S"


def setup_logging() -> None:
    """初始化根 logger，幂等。由应用启动时调用一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """获取带统一格式的 logger。"""
    setup_logging()
    return logging.getLogger(name)
