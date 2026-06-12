"""Unified logging — stdout + rotating file."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config import settings

LOG_DIR = Path(settings.data_dir) / "logs"
LOG_FILE = LOG_DIR / "app.log"


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = []

    # Stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    sh.setLevel(level)
    root.addHandler(sh)

    # Rotating file (10MB, keep 5 backups)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(str(LOG_FILE), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(logging.DEBUG)   # file gets everything
    root.addHandler(fh)
    # 保存引用，shutdown 时主动关闭释放 Windows 文件句柄
    global _file_handler
    _file_handler = fh

    # Quiet noisy third-party
    for name in ("httpx", "httpcore", "urllib3", "openai", "botocore", "multiprocess"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Also route uvicorn logs to our handlers
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        ulog = logging.getLogger(name)
        ulog.handlers = []
        ulog.addHandler(sh)
        ulog.addHandler(fh)
        ulog.setLevel(level)
        ulog.propagate = False

    root.info("Logging to %s", LOG_FILE)


_file_handler: RotatingFileHandler | None = None


def shutdown_logging() -> None:
    """Shutdown: close file handler to release Windows file handle before process exits."""
    global _file_handler
    if _file_handler is not None:
        root = logging.getLogger()
        root.info("Shutting down logging")
        for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
            ulog = logging.getLogger(name)
            ulog.removeHandler(_file_handler)
        root.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
