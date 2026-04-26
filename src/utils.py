from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

from .logger import LOGGER


def load_environment(dotenv_path: str = ".env") -> None:
    try:
        load_dotenv(dotenv_path=dotenv_path)
    except Exception as e:
        LOGGER.error("Failed to load environment file '%s': %s", dotenv_path, e)
        raise


def ensure_directories(paths: Iterable[Path]) -> None:
    try:
        for path in paths:
            path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        LOGGER.error("Failed to create required directories: %s", e)
        raise


def require_env_var(name: str) -> str:
    try:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return value
    except Exception as e:
        LOGGER.error("Environment variable validation failed for '%s': %s", name, e)
        raise
