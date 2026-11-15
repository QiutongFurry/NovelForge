"""Export services for chapters and finished books.

Markdown and plain text are always available; DOCX support uses the optional
``python-docx`` dependency.  All text output is UTF-8 with ``\n`` line endings
so the files open cleanly in every editor on Windows, macOS and Linux.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from novel_ai.domain.models import Chapter, ProjectSnapshot

logger = logging.getLogger(__name__)

#: Characters forbidden by Windows in file names; they are also unsafe on
#: several other platforms, so the sanitiser targets the strictest rule set.
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

_MAX_FILENAME_STEM = 80


class ExportService:
    """Build and write chapter/book exports (Markdown, TXT and optional DOCX)."""

    # ------------------------------------------------------------------
    # Chapter export
    # ------------------------------------------------------------------
    def export_chapter_markdown(
        self, chapter: Chapter, project_title: str, path: Path
    ) -> Path:
        """Write one chapter as Markdown and return the written path."""

        title = (project_title or "").strip() or "未命名作品"
        lines = [f"# {title}", "", f"## {self._chapter_heading(chapter)}", ""]
        content = self._clean_block(chapter.content)
        if content:
            lines.extend([content, ""])
        return self._write_text(path, "\n".join(lines).rstrip() + "\n")

    # ------------------------------------------------------------------
    # Book export
    # ------------------------------------------------------------------
    def build_book_markdown(self, snapshot: ProjectSnapshot) -> str:
        """Build the whole book in Markdown, ordered by chapter number."""

        project = snapshot.project
        title = (project.title or "").strip() or "未命名作品"
        lines = [f"# {title}", ""]

        metadata: list[str] = []
        if project.genre.strip():
            metadata.append(f"- 类型：{project.genre.strip()}")
        if project.logline.strip():
            metadata.append(f"- 一句话简介：{project.logline.strip()}")
        if project.pov.strip():
            metadata.append(f"- 默认视角：{project.pov.strip()}")
        if metadata:
            lines.extend(metadata)
            lines.append("")

        synopsis = self._clean_block(project.synopsis)
        if synopsis:
            lines.extend(["## 简介", "", synopsis, ""])

        for chapter in snapshot.ordered_chapters():
            lines.extend(["---", "", f"## {self._chapter_heading(chapter)}", ""])
            content = self._clean_block(chapter.content)
            if content:
                lines.extend([content, ""])

        return "\n".join(lines).rstrip() + "\n"

    def build_book_txt(self, snapshot: ProjectSnapshot) -> str:
        """Build the whole book as plain text, ordered by chapter number."""

        project = snapshot.project
        title = (project.title or "").strip() or "未命名作品"
        lines = [title, ""]

        if project.genre.strip():
            lines.append(f"类型：{project.genre.strip()}")
        if project.logline.strip():
            lines.append(f"一句话简介：{project.logline.strip()}")
        synopsis = self._clean_block(project.synopsis)
        if synopsis:
            lines.extend(["", "简介：", synopsis])
        lines.append("")

        for chapter in snapshot.ordered_chapters():
            lines.extend([self._chapter_heading(chapter), ""])
            content = self._clean_block(chapter.content)
            if content:
                lines.extend([content, ""])

        return "\n".join(lines).rstrip() + "\n"

    def export_book_markdown(self, snapshot: ProjectSnapshot, path: Path) -> Path:
        return self._write_text(path, self.build_book_markdown(snapshot))

    def export_book_txt(self, snapshot: ProjectSnapshot, path: Path) -> Path:
        return self._write_text(path, self.build_book_txt(snapshot))

    def export_book_docx(self, snapshot: ProjectSnapshot, path: Path) -> Path:
        """Write the book as DOCX.

        Raises a friendly :class:`RuntimeError` when the optional
        ``python-docx`` dependency is not installed.
        """

        try:
            from docx import Document  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "导出 DOCX 需要可选依赖 python-docx，请先安装：pip install python-docx"
            ) from exc

        project = snapshot.project
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)

        document = Document()
        document.add_heading((project.title or "").strip() or "未命名作品", level=0)

        metadata: list[str] = []
        if project.genre.strip():
            metadata.append(f"类型：{project.genre.strip()}")
        if project.logline.strip():
            metadata.append(f"一句话简介：{project.logline.strip()}")
        if metadata:
            document.add_paragraph("　".join(metadata))

        synopsis = self._clean_block(project.synopsis)
        if synopsis:
            document.add_heading("简介", level=1)
            for block in self._paragraph_blocks(synopsis):
                document.add_paragraph(block)

        for chapter in snapshot.ordered_chapters():
            document.add_heading(self._chapter_heading(chapter), level=1)
            for block in self._paragraph_blocks(chapter.content):
                document.add_paragraph(block)

        document.save(str(target))
        logger.info("已导出 DOCX：%s", target)
        return target

    # ------------------------------------------------------------------
    # File names
    # ------------------------------------------------------------------
    def suggested_filename(self, project_title: str, extension: str) -> str:
        """Return a filesystem-safe default file name for an export.

        Windows reserved device names are prefixed with ``_``, forbidden
        characters become ``_``, trailing dots/spaces are removed and overly
        long stems are truncated.
        """

        raw = (project_title or "").strip() or "未命名作品"
        stem = _INVALID_FILENAME_CHARS.sub("_", raw)
        stem = re.sub(r"\s+", " ", stem)
        stem = re.sub(r"_{2,}", "_", stem)
        stem = stem.strip(" ._")
        if not stem:
            stem = "未命名作品"
        if stem.upper() in _WINDOWS_RESERVED_NAMES:
            stem = f"_{stem}"
        if len(stem) > _MAX_FILENAME_STEM:
            stem = stem[:_MAX_FILENAME_STEM].strip(" ._") or "未命名作品"
            if stem.upper() in _WINDOWS_RESERVED_NAMES:
                stem = f"_{stem}"

        clean_extension = _INVALID_FILENAME_CHARS.sub("", (extension or "").strip())
        clean_extension = clean_extension.lstrip(".").strip()
        return f"{stem}.{clean_extension}" if clean_extension else stem

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _chapter_heading(chapter: Chapter) -> str:
        title = (chapter.title or "").strip()
        return f"第{chapter.number}章 {title}" if title else f"第{chapter.number}章"

    @staticmethod
    def _clean_block(text: str) -> str:
        """Trim leading/trailing blank lines but keep paragraph structure."""

        return (text or "").strip("\n\r").rstrip()

    @staticmethod
    def _paragraph_blocks(text: str) -> list[str]:
        # One non-empty line becomes one Word paragraph; this matches Chinese
        # manuscripts that separate paragraphs with single newlines.
        blocks = re.split(r"\n+", (text or "").strip("\n\r"))
        return [block.strip() for block in blocks if block.strip()]

    @staticmethod
    def _write_text(path: Path, text: str) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        logger.debug("已导出文本：%s（%s 字符）", target, len(text))
        return target


__all__ = ["ExportService"]
