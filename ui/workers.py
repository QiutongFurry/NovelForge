"""Background Qt workers.

All network or long-running work runs through these workers with
``QObject.moveToThread``.  They must never execute on the GUI thread.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class GenerationWorker(QObject):
    update = Signal(object)  # GenerationUpdate
    finished = Signal(object)  # GenerationOutcome

    def __init__(
        self,
        generation_service: Any,
        project_id: str,
        chapter_id: str,
        options: Any,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = generation_service
        self._project_id = project_id
        self._chapter_id = chapter_id
        self._options = options
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        outcome: Any
        try:
            outcome = self._service.generate(
                self._project_id,
                self._chapter_id,
                self._options,
                self._cancel_event,
                self._on_update,
            )
        except Exception as exc:  # defensive boundary between worker and GUI
            logger.exception("Unhandled generation worker error")
            outcome = SimpleNamespace(
                ok=False,
                cancelled=False,
                chapter=None,
                plan=None,
                full_text="",
                error=f"生成任务异常终止：{exc}",
                retryable=True,
                memory_updated=False,
                warning="",
            )
        self.finished.emit(outcome)

    def _on_update(self, update: Any) -> None:
        self.update.emit(update)


class RewriteWorker(QObject):
    succeeded = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        generation_service: Any,
        project_id: str,
        chapter_id: str,
        selection: str,
        instruction: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = generation_service
        self._project_id = project_id
        self._chapter_id = chapter_id
        self._selection = selection
        self._instruction = instruction

    def run(self) -> None:
        try:
            text = self._service.rewrite_selection(
                self._project_id,
                self._chapter_id,
                self._selection,
                self._instruction,
            )
        except Exception as exc:
            logger.exception("Local rewrite failed")
            message = getattr(exc, "user_message", None) or f"局部重写失败：{exc}"
            self.failed.emit(str(message))
        else:
            if text.strip():
                self.succeeded.emit(text)
            else:
                self.failed.emit("模型没有返回改写内容，请重试。")
        finally:
            self.finished.emit()


class ProviderTestWorker(QObject):
    finished = Signal(object)  # ProviderTestResult

    def __init__(
        self,
        provider_manager: Any,
        config: Any,
        api_key_override: str | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = provider_manager
        self._config = config
        self._api_key = api_key_override

    def run(self) -> None:
        try:
            result = self._manager.test_connection(
                self._config, api_key_override=self._api_key
            )
        except Exception as exc:
            logger.exception("Provider test crashed")
            result = SimpleNamespace(
                ok=False,
                message=f"连接测试失败：{exc}",
                latency_ms=0,
                model=getattr(self._config, "model", ""),
                detail="",
            )
        self.finished.emit(result)
