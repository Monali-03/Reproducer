"""Jinja2-based template engine for reproducer file generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateNotFound


_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class TemplateEngine:
    """Renders Jinja2 templates for reproducer artifacts."""

    def __init__(self, templates_dir: str | None = None) -> None:
        """Initialize the template engine.

        Args:
            templates_dir: Path to the templates directory.  Defaults to
                ``<project_root>/templates``.
        """
        self._templates_dir = Path(templates_dir) if templates_dir else _TEMPLATES_DIR
        self._templates_dir.mkdir(parents=True, exist_ok=True)

        self._env = Environment(
            loader=FileSystemLoader(str(self._templates_dir)),
            autoescape=False,
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, context: dict[str, Any]) -> str:
        """Render a template with the given context.

        Args:
            template_name: Name of the template file (relative to templates dir).
            context: Variables to pass into the template.

        Returns:
            Rendered string.

        Raises:
            TemplateNotFound: If the template file does not exist.
        """
        template = self._env.get_template(template_name)
        return template.render(**context)

    def get_app_template(self, product: str, app_type: str) -> str:
        """Return the path to an application template based on product and type.

        Args:
            product: Product name (e.g. ``eap``, ``jws``, ``datagrid``).
            app_type: Application type (e.g. ``servlet``, ``ejb``, ``rest``).

        Returns:
            Template name string suitable for :meth:`render`, or an empty string
            if no matching template exists.
        """
        candidates = [
            f"{product}/{app_type}.java.j2",
            f"{product}/default.java.j2",
            f"common/{app_type}.java.j2",
            "common/default.java.j2",
        ]
        for name in candidates:
            template_path = self._templates_dir / name
            if template_path.exists():
                return name
        return ""

    def list_templates(self) -> list[str]:
        """List all available template files.

        Returns:
            Sorted list of template names relative to the templates directory.
        """
        templates: list[str] = []
        if not self._templates_dir.exists():
            return templates
        for path in sorted(self._templates_dir.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                templates.append(str(path.relative_to(self._templates_dir)))
        return templates
