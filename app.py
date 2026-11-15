"""Application bootstrap for 墨言 AI 小说工坊.

Run with ``python -m novel_ai`` or ``python run.py``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PySide6.QtWidgets import QApplication

from novel_ai import APP_NAME, APP_SLUG, __version__
from novel_ai.config.paths import ConfigPaths
from novel_ai.config.secrets import SecretStore
from novel_ai.config.settings import AppSettingsService
from novel_ai.logging_setup import setup_logging
from novel_ai.prompts.templates import PromptStore
from novel_ai.providers.manager import ProviderManager
from novel_ai.services.export_service import ExportService
from novel_ai.services.generation_service import GenerationService
from novel_ai.services.project_service import ProjectService
from novel_ai.services.summary_service import SummaryService
from novel_ai.storage.database import Database
from novel_ai.storage.repository import NovelRepository
from novel_ai.ui.app_context import AppContext
from novel_ai.ui.main_window import MainWindow
from novel_ai.ui.theme import apply_theme

logger = logging.getLogger(__name__)


@dataclass
class AppRuntime:
    app: QApplication
    window: MainWindow
    context: AppContext
    paths: ConfigPaths


def _ensure_application(argv: Sequence[str] | None) -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        app = existing
    else:
        app = QApplication(list(argv) if argv is not None else sys.argv)
    app.setApplicationName(APP_SLUG)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("NovelForge")
    app.setOrganizationDomain("novelforge.local")
    app.setApplicationVersion(__version__)
    return app


def build_runtime(
    data_dir: str | Path | None = None,
    *,
    argv: Sequence[str] | None = None,
    show: bool = True,
) -> AppRuntime:
    """Compose every layer of the application.

    Kept as a function (instead of hidden module-level state) so integration
    tests can create a disposable application in a temporary data directory.
    """

    paths = ConfigPaths.create(data_dir).ensure()
    settings_service = AppSettingsService(paths.settings_path)
    settings = settings_service.load()
    setup_logging(paths.logs_dir, settings.log_level)

    app = _ensure_application(argv)
    apply_theme(app, settings.theme)

    database = Database(paths.database_path)
    database.initialize()
    repo = NovelRepository(database)

    project_service = ProjectService(repo)
    secret_store = SecretStore(paths.secrets_path)
    provider_manager = ProviderManager(repo, secret_store)
    try:
        provider_manager.ensure_seeded()
    except Exception:
        logger.exception("Could not seed default provider configurations")

    prompt_store = PromptStore(paths.prompts_path)
    try:
        prompt_store.load()
    except Exception:
        logger.exception("Could not initialise prompt template store")

    summary_service = SummaryService(repo, provider_manager)
    generation_service = GenerationService(
        repo=repo,
        project_service=project_service,
        provider_manager=provider_manager,
        prompt_store=prompt_store,
        summary_service=summary_service,
        settings=settings,
    )
    export_service = ExportService()
    ctx = AppContext(
        paths=paths,
        settings=settings,
        settings_service=settings_service,
        repo=repo,
        project_service=project_service,
        provider_manager=provider_manager,
        prompt_store=prompt_store,
        export_service=export_service,
        generation_service=generation_service,
        summary_service=summary_service,
    )
    window = MainWindow(ctx)
    if show:
        window.show()
    logger.info(
        "Started %s v%s: data=%s, theme=%s, secret_backend=%s",
        APP_NAME,
        __version__,
        paths.root,
        settings.theme,
        secret_store.backend,
    )
    return AppRuntime(app=app, window=window, context=ctx, paths=paths)


def run(argv: Sequence[str] | None = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    if "--self-test" in args or os.environ.get("NOVEL_AI_SELF_TEST") == "1":
        return run_self_test(args)
    argv = list(argv) if argv is not None else None
    try:
        runtime = build_runtime(argv=argv, show=True)
    except Exception as exc:
        logging.getLogger("novel_ai").exception("Application startup failed")
        try:
            app = QApplication.instance()
            if app is None:
                app = QApplication(list(argv) if argv is not None else sys.argv)
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None,
                f"{APP_NAME} 启动失败",
                "应用初始化失败，请检查数据目录权限和依赖安装。\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                "详细日志位于用户数据目录的 logs/novel_ai.log。",
            )
        except Exception:
            print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    return runtime.app.exec()


def run_self_test(argv: Sequence[str] | None = None) -> int:
    """Headless packaging/diagnostics smoke test.

    Used by the Windows build pipeline to verify that a frozen executable can
    bootstrap Qt, every UI page (including dynamically imported pages), SQLite
    and the provider catalog.  A JSON report is written to the data directory
    unless ``NOVEL_AI_SELF_TEST_REPORT`` names an explicit path.
    """

    report: dict[str, object] = {"ok": False, "pages": {}, "error": ""}
    runtime: AppRuntime | None = None
    try:
        runtime = build_runtime(argv=[sys.argv[0]] if sys.argv else [], show=False)
        from novel_ai.ui.main_window import UnavailablePage

        unavailable: list[str] = []
        page_report: dict[str, str] = {}
        for key, page in runtime.window.pages.items():
            page_report[key] = type(page).__name__
            if isinstance(page, UnavailablePage):
                unavailable.append(key)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()
        providers = runtime.context.provider_manager.list_configs()
        try:
            template_name = runtime.context.prompt_store.load().name
        except Exception as exc:
            template_name = f"error: {exc}"
        try:
            secret_backend = runtime.context.provider_manager.secret_store.backend
        except Exception as exc:
            secret_backend = f"error: {exc}"
        ok = not unavailable and len(page_report) >= 9 and len(providers) >= 1
        report = {
            "ok": ok,
            "pages": page_report,
            "unavailable_pages": unavailable,
            "providers": len(providers),
            "prompt_template": template_name,
            "secret_backend": secret_backend,
            "database_exists": runtime.paths.database_path.exists(),
            "data_dir": str(runtime.paths.root),
            "version": __version__,
            "error": "" if ok else "One or more pages/providers failed to initialise",
        }
        runtime.window.close()
        if app is not None:
            app.processEvents()
        return 0 if ok else 3
    except Exception as exc:  # pragma: no cover - exercised by packaged exe
        logging.getLogger("novel_ai").exception("Self-test failed")
        report["error"] = f"{type(exc).__name__}: {exc}"
        return 2
    finally:
        try:
            paths = runtime.paths if runtime is not None else ConfigPaths.create().ensure()
            configured = os.environ.get("NOVEL_AI_SELF_TEST_REPORT", "").strip()
            report_path = Path(configured) if configured else paths.root / "selftest.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False))
        except Exception:
            logging.getLogger("novel_ai").exception("Could not write self-test report")


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
