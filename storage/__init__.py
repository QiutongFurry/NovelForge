"""SQLite persistence layer: database bootstrap and repository."""

from .database import Database, SCHEMA_VERSION
from .repository import NovelRepository

__all__ = ["Database", "NovelRepository", "SCHEMA_VERSION"]
