"""Create/edit dialog for one AI provider configuration.

The dialog owns no network code: it only collects and validates the fields of a
:class:`~novel_ai.domain.models.ProviderConfig`.  Saving, secret storage and
connection testing are handled by the page/manager so the GUI thread never
blocks on the network.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from novel_ai.domain.models import ProviderConfig, utc_now
from novel_ai.ui.widgets import divider

# Provider catalog is part of the provider-engineer scope and may be missing
# while the project is still being assembled; fall back to a static mirror of
# the documented catalog so this dialog can always be constructed.
try:  # pragma: no cover - import depends on parallel module availability
    from novel_ai.providers.catalog import PROVIDER_CATALOG as _CATALOG_SPECS
    from novel_ai.providers.catalog import spec_for as _catalog_spec_for
except Exception:  # pragma: no cover - defensive fallback
    _CATALOG_SPECS = []
    _catalog_spec_for = None


_FALLBACK_SPEC_DATA: tuple[dict[str, Any], ...] = (
    {
        "provider_type": "openai",
        "display_name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "context_window": 128_000,
        "requires_api_key": True,
        "description": "OpenAI 官方 API。",
    },
    {
        "provider_type": "deepseek",
        "display_name": "DeepSeek 深度求索",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "context_window": 128_000,
        "requires_api_key": True,
        "description": "DeepSeek 官方 API。",
    },
    {
        "provider_type": "qwen",
        "display_name": "通义千问 Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "context_window": 131_072,
        "requires_api_key": True,
        "description": "阿里云百炼 OpenAI 兼容接口。",
    },
    {
        "provider_type": "kimi",
        "display_name": "Kimi 月之暗面",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-32k",
        "context_window": 32_768,
        "requires_api_key": True,
        "description": "Moonshot OpenAI 兼容接口。",
    },
    {
        "provider_type": "anthropic",
        "display_name": "Anthropic Claude",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-sonnet-4-5",
        "context_window": 200_000,
        "requires_api_key": True,
        "description": "Anthropic Messages API。",
    },
    {
        "provider_type": "gemini",
        "display_name": "Google Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.0-flash",
        "context_window": 1_048_576,
        "requires_api_key": True,
        "description": "Google Gemini 官方 API。",
    },
    {
        "provider_type": "ollama",
        "display_name": "Ollama 本地",
        "base_url": "http://localhost:11434/v1",
        "default_model": "llama3.1",
        "context_window": 32_768,
        "requires_api_key": False,
        "description": "本地 Ollama（OpenAI 兼容），无需密钥。",
    },
    {
        "provider_type": "openai_compatible",
        "display_name": "自定义 OpenAI 兼容",
        "base_url": "http://localhost:8000/v1",
        "default_model": "gpt-4o-mini",
        "context_window": 32_768,
        "requires_api_key": False,
        "description": "任意 OpenAI 兼容服务。",
    },
)

_FALLBACK_SPECS: list[SimpleNamespace] = [
    SimpleNamespace(**data) for data in _FALLBACK_SPEC_DATA
]


def load_provider_specs() -> list[Any]:
    """Return catalog specs, or the built-in fallback when unavailable."""

    specs = list(_CATALOG_SPECS) if _CATALOG_SPECS else []
    return specs or list(_FALLBACK_SPECS)


def spec_for_provider_type(provider_type: str | None) -> Any | None:
    """Look up a provider spec, tolerating aliases and a missing catalog."""

    if not provider_type:
        return None
    if _catalog_spec_for is not None:
        try:
            spec = _catalog_spec_for(provider_type)
        except Exception:  # pragma: no cover - defensive
            spec = None
        if spec is not None:
            return spec
    wanted = str(provider_type).strip().lower()
    for spec in _FALLBACK_SPECS:
        if spec.provider_type == wanted:
            return spec
    return None


def provider_type_label(provider_type: str) -> str:
    """Return a human readable label for a provider type."""

    spec = spec_for_provider_type(provider_type)
    if spec is not None:
        return str(getattr(spec, "display_name", "") or provider_type)
    return provider_type or "未知供应商"


class ProviderDialog(QDialog):
    """Modal editor for one :class:`ProviderConfig`.

    Usage::

        dialog = ProviderDialog(parent, config=existing, has_existing_key=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config = dialog.result_config()
            api_key = dialog.api_key()  # None means "keep the stored key"
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        config: ProviderConfig | None = None,
        has_existing_key: bool = False,
        masked_key: str = "未配置",
    ) -> None:
        super().__init__(parent)
        self._current = config
        self._specs = load_provider_specs()
        self._has_existing_key = bool(has_existing_key)
        self._masked_key = masked_key or "未配置"
        self._result: ProviderConfig | None = None
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setWindowTitle("编辑供应商" if config is not None else "添加供应商")
        self._build_ui()
        self._load(config)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        root.setSpacing(12)

        heading = QLabel("供应商配置")
        heading.setStyleSheet("font-size: 17px; font-weight: 600;")
        root.addWidget(heading)
        intro = QLabel(
            "API Key 只保存在本机安全存储（系统钥匙串或仅当前用户可读的本地文件），"
            "不会写入项目数据库、导出文件或日志。"
        )
        intro.setProperty("role", "muted")
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：DeepSeek 主力")
        self.type_combo = QComboBox()
        for spec in self._specs:
            self.type_combo.addItem(
                str(getattr(spec, "display_name", "") or getattr(spec, "provider_type", "")),
                str(getattr(spec, "provider_type", "")),
            )
        for spec in self._specs:
            description = str(getattr(spec, "description", "") or "")
            if description:
                index = self.type_combo.findData(getattr(spec, "provider_type", ""))
                if index >= 0:
                    self.type_combo.setItemData(
                        index, description, Qt.ItemDataRole.ToolTipRole
                    )

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.example.com/v1")
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("输入 API Key（留空则保留已保存的密钥）")
        self.show_key_check = QCheckBox("显示")
        self.show_key_check.toggled.connect(self._toggle_key_visibility)
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(self.show_key_check)
        key_box = QWidget()
        key_box.setLayout(key_row)
        key_row.setContentsMargins(0, 0, 0, 0)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("例如：deepseek-chat")
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.05)
        self.temperature_spin.setDecimals(2)
        self.temperature_spin.setValue(0.8)
        self.temperature_spin.setToolTip("越高越有创造力，越低越稳定；创作建议 0.6 ~ 1.0。")
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(64, 2_000_000)
        self.max_tokens_spin.setSingleStep(256)
        self.max_tokens_spin.setValue(4096)
        self.max_tokens_spin.setSuffix(" tokens")
        self.context_window_spin = QSpinBox()
        self.context_window_spin.setRange(1024, 4_000_000)
        self.context_window_spin.setSingleStep(1024)
        self.context_window_spin.setValue(128_000)
        self.context_window_spin.setSuffix(" tokens")
        self.context_window_spin.setToolTip("模型支持的最大上下文窗口，用于计算可用输入预算。")
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(5.0, 600.0)
        self.timeout_spin.setSingleStep(5.0)
        self.timeout_spin.setDecimals(0)
        self.timeout_spin.setValue(120.0)
        self.timeout_spin.setSuffix(" 秒")
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("可选，例如 http://127.0.0.1:7890")
        self.stream_check = QCheckBox("启用流式输出（SSE，推荐）")
        self.stream_check.setChecked(True)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.addRow("名称", self.name_edit)
        form.addRow("供应商类型", self.type_combo)
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("API Key", key_box)
        form.addRow("模型", self.model_edit)
        form.addRow("温度", self.temperature_spin)
        form.addRow("最大输出", self.max_tokens_spin)
        form.addRow("上下文窗口", self.context_window_spin)
        form.addRow("超时", self.timeout_spin)
        form.addRow("代理", self.proxy_edit)
        form.addRow("", self.stream_check)
        root.addLayout(form)

        self.key_hint = QLabel()
        self.key_hint.setProperty("role", "hint")
        self.key_hint.setWordWrap(True)
        root.addWidget(self.key_hint)
        root.addWidget(divider())

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #FF8296;")
        self.error_label.setVisible(False)
        root.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton("保存")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        root.addLayout(buttons)

        for edit in (self.name_edit, self.base_url_edit, self.model_edit):
            edit.textChanged.connect(
                lambda _text, widget=edit: self._set_field_error(widget, False)
            )

    # --------------------------------------------------------------- loading
    def _load(self, config: ProviderConfig | None) -> None:
        if config is not None:
            self._current = config
            index = self.type_combo.findData(config.provider_type)
            if index < 0:
                self.type_combo.addItem(
                    f"自定义（{config.provider_type}）", config.provider_type
                )
                index = self.type_combo.findData(config.provider_type)
            self.type_combo.setCurrentIndex(max(0, index))
            self.name_edit.setText(config.name)
            self.base_url_edit.setText(config.base_url)
            self.model_edit.setText(config.model)
            self.temperature_spin.setValue(float(config.temperature))
            self.max_tokens_spin.setValue(max(64, int(config.max_tokens)))
            self.context_window_spin.setValue(max(1024, int(config.context_window)))
            self.timeout_spin.setValue(float(config.timeout))
            self.proxy_edit.setText(config.proxy)
            self.stream_check.setChecked(bool(config.stream))
            self.setWindowTitle("编辑供应商")
        else:
            if self._specs:
                self.type_combo.setCurrentIndex(0)
                self._apply_spec(self._specs[0], fill_name=True)
            self.setWindowTitle("添加供应商")
        self._update_key_hint()
        self.api_key_edit.setPlaceholderText(
            "留空则保留已保存的密钥（输入新值则替换）"
            if self._has_existing_key
            else "输入 API Key（可稍后补充）"
        )

    def _apply_spec(self, spec: Any, *, fill_name: bool = False) -> None:
        self.base_url_edit.setText(str(getattr(spec, "base_url", "") or ""))
        self.model_edit.setText(str(getattr(spec, "default_model", "") or ""))
        context_window = int(getattr(spec, "context_window", 128_000) or 128_000)
        self.context_window_spin.setValue(max(1024, context_window))
        display_name = str(getattr(spec, "display_name", "") or "")
        current_name = self.name_edit.text().strip()
        known_names = {
            str(getattr(item, "display_name", "") or "") for item in self._specs
        }
        if fill_name or not current_name or current_name in known_names:
            self.name_edit.setText(display_name)
        self._update_key_hint()

    def _on_type_changed(self, _index: int) -> None:
        provider_type = str(self.type_combo.currentData() or "")
        spec = spec_for_provider_type(provider_type)
        if spec is None:
            self._update_key_hint()
            return
        self._apply_spec(spec, fill_name=False)
        self._set_field_error(self.base_url_edit, False)
        self._set_field_error(self.model_edit, False)

    def _update_key_hint(self) -> None:
        spec = spec_for_provider_type(str(self.type_combo.currentData() or ""))
        requires_key = True if spec is None else bool(
            getattr(spec, "requires_api_key", True)
        )
        if self._has_existing_key:
            self.key_hint.setText(
                f"已保存密钥：{self._masked_key}。留空将保留现有密钥；输入新值则替换。"
            )
        elif not requires_key:
            self.key_hint.setText("该供应商通常无需 API Key（本地服务可留空）。")
        else:
            self.key_hint.setText(
                "密钥将写入本机安全存储；留空表示暂不配置，可稍后再补充。"
            )

    def _toggle_key_visibility(self, visible: bool) -> None:
        self.api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )

    # ------------------------------------------------------------ validation
    def _set_field_error(self, widget: QLineEdit, has_error: bool) -> None:
        widget.setProperty("error", has_error)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _validate_and_build(self) -> ProviderConfig | None:
        name = self.name_edit.text().strip()
        base_url = self.base_url_edit.text().strip()
        model = self.model_edit.text().strip()
        errors: list[str] = []
        self._set_field_error(self.name_edit, not name)
        self._set_field_error(self.base_url_edit, not base_url)
        self._set_field_error(self.model_edit, not model)
        if not name:
            errors.append("请填写供应商名称。")
        if not base_url:
            errors.append("请填写 Base URL。")
        if not model:
            errors.append("请填写模型名称。")
        if errors:
            self._show_error(" ".join(errors))
            return None
        self._show_error("")
        provider_type = str(self.type_combo.currentData() or "openai_compatible")
        values: dict[str, Any] = {
            "name": name,
            "provider_type": provider_type,
            "base_url": base_url,
            "model": model,
            "temperature": round(float(self.temperature_spin.value()), 2),
            "max_tokens": int(self.max_tokens_spin.value()),
            "context_window": int(self.context_window_spin.value()),
            "timeout": float(self.timeout_spin.value()),
            "proxy": self.proxy_edit.text().strip(),
            "stream": self.stream_check.isChecked(),
        }
        if self._current is None:
            return ProviderConfig.create(**values)
        return replace(self._current, **values, updated_at=utc_now())

    # ---------------------------------------------------------------- result
    def accept(self) -> None:  # noqa: D102 - Qt override
        config = self._validate_and_build()
        if config is None:
            return
        self._result = config
        super().accept()

    def result_config(self) -> ProviderConfig | None:
        """Return the edited config after the dialog was accepted."""

        return self._result

    def api_key(self) -> str | None:
        """Return the newly typed key, or ``None`` to keep the stored one."""

        text = self.api_key_edit.text().strip()
        return text or None

    @property
    def config_id(self) -> str | None:
        return self._current.id if self._current is not None else None


__all__ = [
    "ProviderDialog",
    "load_provider_specs",
    "provider_type_label",
    "spec_for_provider_type",
]
