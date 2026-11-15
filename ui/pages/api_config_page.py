"""AI provider configuration page.

Shows every configured provider as a card with model, endpoint, masked key and
default state.  Buttons open the provider dialog, delete/set-default through the
manager and run a real connection test on a background ``QThread`` so the GUI is
never blocked by a network request.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import ProviderConfig
from novel_ai.ui.dialogs.provider_dialog import (
    ProviderDialog,
    provider_type_label,
    spec_for_provider_type,
)
from novel_ai.ui.icons import icon
from novel_ai.ui.pages.base import BasePage
from novel_ai.ui.widgets import (
    Badge,
    Card,
    ConfirmDialog,
    EmptyState,
    MutedLabel,
    PageHeader,
    ScrollPage,
    SectionTitle,
    StatCard,
)
from novel_ai.ui.workers import ProviderTestWorker

logger = logging.getLogger(__name__)


class ApiConfigPage(BasePage):
    page_key = "api"
    page_title = "AI 配置"
    page_subtitle = "管理 AI 供应商、模型参数与 API Key；网络请求全部在后台线程执行。"

    def __init__(self, ctx, parent: QWidget | None = None) -> None:  # noqa: ANN001
        super().__init__(ctx, parent)
        self._test_thread: QThread | None = None
        self._test_worker: ProviderTestWorker | None = None
        self._test_provider_id: str = ""
        self.test_results: dict[str, Any] = {}
        self.test_buttons: dict[str, QPushButton] = {}
        self.empty_state: EmptyState | None = None
        self._build_ui()
        self._connect_signals()
        self.refresh()

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = ScrollPage(self, margins=(24, 20, 24, 28), spacing=16)
        root.addWidget(self.scroll)

        self.header = PageHeader(self.page_title, self.page_subtitle)
        self.refresh_button = self._button("刷新", "refresh", self.refresh)
        self.add_button = self._button(
            "添加供应商", "plus", self._add_provider, variant="primary"
        )
        self.header.add_action(self.refresh_button)
        self.header.add_action(self.add_button)
        self.scroll.add(self.header)

        stats = QHBoxLayout()
        stats.setSpacing(14)
        self.provider_count_card = StatCard("供应商数量", "0", "加载中…")
        self.default_provider_card = StatCard("默认供应商", "未设置", "生成时优先使用")
        self.secret_backend_card = StatCard("密钥存储", "检测中", "本机安全存储后端")
        for card in (
            self.provider_count_card,
            self.default_provider_card,
            self.secret_backend_card,
        ):
            stats.addWidget(card, 1)
        self.scroll.add_layout(stats)

        self.secret_card = Card(padding=14, spacing=8)
        secret_header = QHBoxLayout()
        secret_header.setSpacing(8)
        secret_header.addWidget(SectionTitle("API Key 安全存储"))
        secret_header.addStretch(1)
        self.secret_badge = Badge("检测中", "neutral")
        secret_header.addWidget(self.secret_badge)
        self.secret_card.content_layout.addLayout(secret_header)
        self.secret_hint = MutedLabel("正在读取密钥存储后端信息…")
        self.secret_card.content_layout.addWidget(self.secret_hint)
        self.scroll.add(self.secret_card)

        self.test_card = Card(padding=14, spacing=8)
        test_header = QHBoxLayout()
        test_header.setSpacing(8)
        test_header.addWidget(SectionTitle("连接测试"))
        test_header.addStretch(1)
        self.test_badge = Badge("未测试", "neutral")
        test_header.addWidget(self.test_badge)
        self.test_card.content_layout.addLayout(test_header)
        self.test_status = MutedLabel(
            "添加供应商后，可在对应卡片中点击「测试连接」验证 API Key 与网络。"
        )
        self.test_card.content_layout.addWidget(self.test_status)
        self.scroll.add(self.test_card)

        self.scroll.add(SectionTitle("供应商"))
        self.provider_container = QWidget()
        self.provider_layout = QVBoxLayout(self.provider_container)
        self.provider_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_layout.setSpacing(12)
        self.scroll.add(self.provider_container)
        self.scroll.body_layout.addStretch(1)

    def _button(
        self,
        text: str,
        icon_name: str,
        handler,
        *,
        variant: str = "secondary",
    ) -> QPushButton:
        button = QPushButton(text)
        if icon_name:
            button.setIcon(icon(icon_name))
        button.setProperty("variant", variant)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(handler)
        return button

    def _connect_signals(self) -> None:
        try:
            self.ctx.providerChanged.connect(self.refresh)
        except Exception:  # pragma: no cover - context always exposes the signal
            logger.warning("AppContext.providerChanged is unavailable")

    # -------------------------------------------------------------- refresh
    def refresh(self) -> None:
        try:
            self._refresh_provider_cards()
        except Exception:  # pragma: no cover - defensive UI boundary
            logger.exception("Could not refresh the API configuration page")

    def _refresh_provider_cards(self) -> None:
        configs: list[ProviderConfig] = []
        error = ""
        try:
            configs = list(self.ctx.provider_manager.list_configs())
        except Exception as exc:
            error = str(exc)
            logger.warning("Could not list provider configs: %s", exc)

        default = next((config for config in configs if config.is_default), None)
        key_info: dict[str, tuple[bool, str]] = {
            config.id: self._key_info(config.id) for config in configs
        }
        configured = sum(1 for known, _masked in key_info.values() if known)

        self.provider_count_card.set_value(
            str(len(configs)),
            f"已配置密钥 {configured} 个" if configs else "首次使用请先添加供应商",
        )
        if default is not None:
            self.default_provider_card.set_value(
                default.name or "未命名供应商",
                f"{provider_type_label(default.provider_type)} · {default.model or '未填写模型'}",
            )
        else:
            self.default_provider_card.set_value(
                "未设置",
                "生成时会回退到列表中的第一个供应商" if configs else "生成前需要设置默认供应商",
            )
        self._refresh_secret_card()

        self._clear_provider_layout()
        if error:
            self.empty_state = None
            self.provider_layout.addWidget(self._build_error_card(error))
            return
        if not configs:
            self._build_empty_state()
            return
        self.empty_state = None
        for config in configs:
            known, masked = key_info.get(config.id, (False, "未配置"))
            self.provider_layout.addWidget(
                self._build_provider_card(
                    config,
                    is_default=default is not None and default.id == config.id,
                    key_configured=known,
                    masked_key=masked,
                )
            )

    def _clear_provider_layout(self) -> None:
        self.empty_state = None
        while self.provider_layout.count():
            item = self.provider_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.test_buttons.clear()

    def _refresh_secret_card(self) -> None:
        manager = getattr(self.ctx, "provider_manager", None)
        store = getattr(manager, "secret_store", None)
        backend = str(getattr(store, "backend", "") or "")
        if backend == "keyring":
            self.secret_backend_card.set_value("系统钥匙串", "由操作系统加密保管")
            self.secret_backend_card.hint_label.setToolTip("")
            self.secret_badge.setText("安全")
            self.secret_badge.set_variant("success")
            self.secret_hint.setText(
                "当前使用操作系统钥匙串保存 API Key，密钥不会写入项目数据库、导出文件或日志。"
            )
        elif backend == "local-file":
            self.secret_backend_card.set_value("本地文件", "权限仅限当前用户")
            path = getattr(store, "fallback_path", None)
            hint = (
                "未检测到可用的系统钥匙串，API Key 保存在仅当前用户可读的本地文件中"
                "（安全性弱于系统钥匙串）。"
            )
            if path:
                hint += f"\n文件位置：{path}"
                self.secret_backend_card.hint_label.setToolTip(str(path))
            self.secret_badge.setText("降级存储")
            self.secret_badge.set_variant("warning")
            self.secret_hint.setText(hint)
        else:
            self.secret_backend_card.set_value("未知", "无法读取密钥后端")
            self.secret_badge.setText("未知")
            self.secret_badge.set_variant("neutral")
            self.secret_hint.setText(
                "无法获取密钥存储后端信息；如果保存密钥失败，请检查系统钥匙串是否可用。"
            )

    # -------------------------------------------------------------- cards
    def _build_error_card(self, error: str) -> Card:
        card = Card(padding=16, spacing=10)
        title = QLabel("读取供应商配置失败")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        card.content_layout.addWidget(title)
        card.content_layout.addWidget(MutedLabel(f"{error}"))
        retry = QPushButton("重试")
        retry.setProperty("variant", "secondary")
        retry.clicked.connect(lambda: self.refresh())
        card.content_layout.addWidget(retry, alignment=Qt.AlignmentFlag.AlignLeft)
        return card

    def _build_empty_state(self) -> None:
        self.empty_state = EmptyState(
            "还没有配置 AI 供应商",
            "添加一个供应商并填写 API Key 后即可开始 AI 写作。"
            "DeepSeek、通义千问、Kimi 等中文模型性价比更高；本地 Ollama 无需密钥。",
            action_text="添加第一个供应商",
            icon_name="key",
        )
        self.empty_state.actionClicked.connect(self._add_provider)
        self.provider_layout.addWidget(self.empty_state)

        guide = Card(padding=16, spacing=8)
        guide.content_layout.addWidget(SectionTitle("三步开始 AI 写作"))
        steps = QLabel(
            "1. 选择供应商类型：自动填充 Base URL、默认模型与上下文窗口。\n"
            "2. 填写在供应商控制台申请的 API Key（只保存在本机）。\n"
            "3. 保存后点击「测试连接」，成功后设为默认供应商。"
        )
        steps.setProperty("role", "muted")
        steps.setWordWrap(True)
        guide.content_layout.addWidget(steps)
        self.provider_layout.addWidget(guide)

    def _build_provider_card(
        self,
        config: ProviderConfig,
        *,
        is_default: bool,
        key_configured: bool,
        masked_key: str,
    ) -> Card:
        card = Card(padding=16, spacing=10)

        header = QHBoxLayout()
        header.setSpacing(8)
        name_label = QLabel(config.name or "未命名供应商")
        name_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        header.addWidget(name_label)
        header.addWidget(Badge(provider_type_label(config.provider_type), "neutral"))
        if is_default:
            header.addWidget(Badge("默认", "success"))
        requires_key = self._requires_api_key(config.provider_type)
        if not key_configured and requires_key:
            header.addWidget(Badge("未配置密钥", "warning"))
        header.addStretch(1)

        test_button = QPushButton("测试连接")
        test_button.setProperty("variant", "secondary")
        test_button.setEnabled(self._test_thread is None)
        edit_button = QPushButton("编辑")
        edit_button.setProperty("variant", "secondary")
        if is_default:
            default_button = QPushButton("已是默认")
            default_button.setEnabled(False)
        else:
            default_button = QPushButton("设为默认")
            default_button.setProperty("variant", "ghost")
        delete_button = QPushButton("删除")
        delete_button.setProperty("variant", "danger")
        for button in (test_button, edit_button, default_button, delete_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            header.addWidget(button)
        card.content_layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        rows: list[tuple[str, str]] = [
            ("模型", config.model or "未填写"),
            ("Base URL", config.base_url or "未填写"),
            ("API Key", masked_key),
            (
                "生成参数",
                f"温度 {config.temperature:g} · 最大输出 {config.max_tokens:,} tokens"
                f" · 上下文 {config.context_window:,} tokens",
            ),
            (
                "网络",
                f"超时 {config.timeout:g} 秒"
                + (f" · 代理 {config.proxy}" if config.proxy else " · 直连"),
            ),
            ("流式输出", "已开启" if config.stream else "已关闭"),
        ]
        for row, (label, value) in enumerate(rows):
            key_label = QLabel(label)
            key_label.setProperty("role", "caption")
            value_label = QLabel(value)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            if label in ("Base URL", "网络"):
                value_label.setToolTip(value)
            grid.addWidget(key_label, row, 0)
            grid.addWidget(value_label, row, 1)
        grid.setColumnStretch(1, 1)
        card.content_layout.addLayout(grid)

        if not key_configured and requires_key:
            card.content_layout.addWidget(
                MutedLabel("尚未配置 API Key：生成前请点击「编辑」补充密钥。")
            )
        elif not requires_key:
            card.content_layout.addWidget(
                MutedLabel("该供应商类型通常无需 API Key，可直接测试连接。")
            )

        result = self.test_results.get(config.id)
        if result is not None:
            status_row = QHBoxLayout()
            status_row.setSpacing(8)
            ok = bool(getattr(result, "ok", False))
            status_row.addWidget(
                Badge("连接成功" if ok else "连接失败", "success" if ok else "danger")
            )
            status_row.addWidget(MutedLabel(self._describe_test_result(result)), 1)
            card.content_layout.addLayout(status_row)

        test_button.clicked.connect(
            lambda _checked=False, provider_id=config.id: self.start_test_connection(
                provider_id
            )
        )
        edit_button.clicked.connect(
            lambda _checked=False, provider_id=config.id: self._edit_provider(
                provider_id
            )
        )
        default_button.clicked.connect(
            lambda _checked=False, provider_id=config.id: self._set_default_provider(
                provider_id
            )
        )
        delete_button.clicked.connect(
            lambda _checked=False, provider_id=config.id: self._delete_provider(
                provider_id
            )
        )
        self.test_buttons[config.id] = test_button
        return card

    # ------------------------------------------------------------ helpers
    def _get_config(self, provider_id: str) -> ProviderConfig | None:
        try:
            return self.ctx.provider_manager.get_config(provider_id)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not load provider config %s: %s", provider_id, exc)
            return None

    @staticmethod
    def _requires_api_key(provider_type: str) -> bool:
        spec = spec_for_provider_type(provider_type)
        return True if spec is None else bool(
            getattr(spec, "requires_api_key", True)
        )

    def _key_info(self, provider_id: str) -> tuple[bool, str]:
        """Return ``(configured, masked_key)`` with a single secret lookup."""

        try:
            masked = str(self.ctx.provider_manager.masked_key(provider_id))
        except Exception:
            return False, "未配置"
        return masked != "未配置", masked

    @staticmethod
    def _describe_test_result(result: Any) -> str:
        ok = bool(getattr(result, "ok", False))
        latency = int(getattr(result, "latency_ms", 0) or 0)
        model = str(getattr(result, "model", "") or "")
        message = str(getattr(result, "message", "") or "")
        detail = str(getattr(result, "detail", "") or "")
        parts = [message or ("连接成功。" if ok else "连接失败。")]
        if ok and latency:
            parts.append(f"延迟 {latency} ms")
        if model:
            parts.append(f"模型 {model}")
        if not ok and detail and detail not in message:
            parts.append(detail)
        return " · ".join(parts)

    def _notify_provider_change(self) -> None:
        try:
            self.ctx.providerChanged.emit()
        except Exception:  # pragma: no cover - defensive
            logger.warning("Could not emit providerChanged")
            self.refresh()

    # ------------------------------------------------------------ actions
    def _add_provider(self) -> None:
        dialog = ProviderDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        config = dialog.result_config()
        if config is None:
            return
        api_key = dialog.api_key()
        has_existing = False
        try:
            has_existing = bool(self.ctx.provider_manager.list_configs())
        except Exception:
            has_existing = False
        if not has_existing:
            config.is_default = True
        try:
            saved = self.ctx.provider_manager.save_config(config, api_key=api_key)
        except Exception as exc:
            logger.exception("Could not save provider config")
            self.ctx.notify(f"保存供应商失败：{exc}", "error")
            return
        self.test_results.pop(saved.id, None)
        self._notify_saved(saved, is_default=config.is_default)
        self._notify_provider_change()

    def _edit_provider(self, provider_id: str) -> None:
        config = self._get_config(provider_id)
        if config is None:
            self.ctx.notify("供应商配置不存在，可能已被删除。", "error")
            return
        has_key, masked = self._key_info(provider_id)
        dialog = ProviderDialog(
            self,
            config=config,
            has_existing_key=has_key,
            masked_key=masked,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dialog.result_config()
        if updated is None:
            return
        try:
            saved = self.ctx.provider_manager.save_config(
                updated, api_key=dialog.api_key()
            )
        except Exception as exc:
            logger.exception("Could not update provider config")
            self.ctx.notify(f"更新供应商失败：{exc}", "error")
            return
        self.test_results.pop(provider_id, None)
        self._notify_saved(saved, is_default=saved.is_default)
        self._notify_provider_change()

    def _notify_saved(self, saved: ProviderConfig, *, is_default: bool) -> None:
        key_known = self._key_info(saved.id)[0]
        if self._requires_api_key(saved.provider_type) and not key_known:
            self.ctx.notify(
                f"供应商「{saved.name}」已保存；尚未配置 API Key，"
                "生成前请点击「编辑」补充。",
                "warning",
            )
        elif is_default:
            self.ctx.notify(
                f"供应商「{saved.name}」已保存，并设为默认供应商。", "success"
            )
        else:
            self.ctx.notify(f"供应商「{saved.name}」已保存。", "success")

    def _set_default_provider(self, provider_id: str) -> None:
        config = self._get_config(provider_id)
        if config is None:
            self.ctx.notify("供应商配置不存在，可能已被删除。", "error")
            return
        try:
            self.ctx.provider_manager.set_default(provider_id)
        except Exception as exc:
            logger.exception("Could not set default provider")
            self.ctx.notify(f"设置默认供应商失败：{exc}", "error")
            return
        self.ctx.notify(f"已将「{config.name}」设为默认供应商。", "success")
        self._notify_provider_change()

    def _delete_provider(self, provider_id: str) -> None:
        config = self._get_config(provider_id)
        name = config.name if config is not None else provider_id
        confirmed = ConfirmDialog.ask(
            self,
            "删除供应商",
            f"确定要删除「{name}」吗？\n对应的 API Key 也会从本机安全存储中移除，"
            "此操作不可撤销。",
            confirm_text="删除",
            danger=True,
        )
        if not confirmed:
            return
        try:
            self.ctx.provider_manager.delete_config(provider_id)
        except Exception as exc:
            logger.exception("Could not delete provider config")
            self.ctx.notify(f"删除供应商失败：{exc}", "error")
            return
        self.test_results.pop(provider_id, None)
        self.ctx.notify(f"供应商「{name}」已删除。", "success")
        self._notify_provider_change()

    # ------------------------------------------------------- test connection
    def start_test_connection(self, provider_id: str) -> None:
        """Run ``ProviderTestWorker`` on a dedicated thread (never on the GUI)."""

        if self._test_thread is not None:
            self.ctx.notify("连接测试正在进行中，请稍候。", "warning")
            return
        config = self._get_config(provider_id)
        if config is None:
            self.ctx.notify("供应商配置不存在，可能已被删除。", "error")
            return

        thread = QThread()
        worker = ProviderTestWorker(self.ctx.provider_manager, config)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_test_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_test_thread_finished)
        thread.finished.connect(thread.deleteLater)

        self._test_thread = thread
        self._test_worker = worker
        self._test_provider_id = provider_id
        self._set_testing(True, config.name)
        thread.start()

    def _set_testing(self, testing: bool, name: str = "") -> None:
        if testing:
            self.test_badge.setText("测试中")
            self.test_badge.set_variant("warning")
            self.test_status.setText(
                f"正在后台测试「{name}」的连接，界面保持可操作…"
            )
            self._set_test_buttons_enabled(False)
        else:
            self._set_test_buttons_enabled(True)

    def _set_test_buttons_enabled(self, enabled: bool) -> None:
        for button in self.test_buttons.values():
            try:
                button.setEnabled(enabled)
            except RuntimeError:  # deleted C++ object after a refresh
                continue

    def _on_test_finished(self, result: Any) -> None:
        provider_id = self._test_provider_id
        if provider_id:
            self.test_results[provider_id] = result
        self._update_test_banner(result)
        ok = bool(getattr(result, "ok", False))
        latency = int(getattr(result, "latency_ms", 0) or 0)
        if ok:
            self.ctx.notify(f"连接测试成功，延迟 {latency} ms。", "success")
        else:
            self.ctx.notify(
                f"连接测试失败：{getattr(result, 'message', '') or '未知错误'}",
                "error",
            )
        try:
            self._refresh_provider_cards()
        except Exception:  # pragma: no cover - defensive
            logger.exception("Could not refresh provider cards after a test")
        self._set_test_buttons_enabled(False)

    def _on_test_thread_finished(self) -> None:
        self._test_thread = None
        self._test_worker = None
        self._test_provider_id = ""
        self._set_test_buttons_enabled(True)

    def _update_test_banner(self, result: Any) -> None:
        ok = bool(getattr(result, "ok", False))
        self.test_badge.setText("成功" if ok else "失败")
        self.test_badge.set_variant("success" if ok else "danger")
        detail = str(getattr(result, "detail", "") or "")
        self.test_status.setText(self._describe_test_result(result))
        self.test_status.setToolTip(detail)

    # ------------------------------------------------------------ lifecycle
    def on_project_changed(self, project) -> None:  # noqa: ANN001 - BasePage hook
        self.refresh()

    def on_navigated(self, payload: object = None) -> None:
        self.refresh()

    def has_running_task(self) -> bool:
        return self._test_thread is not None

    def shutdown(self, wait_ms: int = 1200) -> bool:
        """Best-effort wait for an in-flight connection test before app exit."""

        thread = self._test_thread
        if thread is None or not thread.isRunning():
            return True
        return thread.wait(wait_ms)
