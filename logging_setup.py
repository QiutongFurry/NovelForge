"""Central logging configuration with rotating file output."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("novel_ai")
    numeric = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(numeric)
    root.propagate = False

    # Avoid duplicate handlers when called repeatedly (tests, app restart).
    for handler in list(root.handlers):
        if getattr(handler, "_novel_ai_handler", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(
        log_dir / "novel_ai.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler._novel_ai_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    console._novel_ai_handler = True  # type: ignore[attr-defined]
    root.addHandler(console)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"novel_ai.{name}")
