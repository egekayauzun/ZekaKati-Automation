from __future__ import annotations

import logging
from pathlib import Path


LOGGER_NAME = "youtube_automation"
LOGGER = logging.getLogger(LOGGER_NAME)


def setup_logging(level: int = logging.INFO, log_file: str = "logs/automation.log") -> logging.Logger:
    """Configure logging to console and file handlers."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
