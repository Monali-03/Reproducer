"""Validator for generated reproducer packages."""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


class ReproducerValidator:
    """Validates the structure and content of generated reproducer packages."""

    def validate(self, reproducer_dir: str) -> dict[str, Any]:
        """Validate a generated reproducer directory.

        Args:
            reproducer_dir: Path to the reproducer directory.

        Returns:
            Dict with keys ``valid`` (bool), ``warnings`` (list[str]),
            and ``errors`` (list[str]).
        """
        base = Path(reproducer_dir)
        errors: list[str] = []
        warnings: list[str] = []

        if not base.exists():
            return {
                "valid": False,
                "errors": [f"Directory does not exist: {reproducer_dir}"],
                "warnings": [],
            }

        # Run all checks
        self._check_xml_files(base, errors, warnings)
        self._check_yaml_files(base, errors, warnings)
        self._check_java_files(base, errors, warnings)
        self._check_scripts(base, errors, warnings)
        self._check_file_references(base, errors, warnings)

        # Detect expected namespace and check consistency
        namespace = self._detect_namespace(base)
        if namespace:
            self._check_namespace_consistency(base, namespace, errors, warnings)

        # Check that essential files exist
        self._check_essential_files(base, errors, warnings)

        return {
            "valid": len(errors) == 0,
            "warnings": warnings,
            "errors": errors,
        }

    def _check_xml_files(
        self,
        base: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Validate XML file syntax."""
        for xml_file in base.rglob("*.xml"):
            rel = xml_file.relative_to(base)
            try:
                ET.parse(xml_file)
            except ET.ParseError as e:
                errors.append(f"XML syntax error in {rel}: {e}")
            except Exception as e:
                warnings.append(f"Could not parse XML {rel}: {e}")

    def _check_yaml_files(
        self,
        base: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Validate YAML file syntax."""
        yaml_extensions = {".yaml", ".yml"}
        for yaml_file in base.rglob("*"):
            if yaml_file.suffix not in yaml_extensions:
                continue
            rel = yaml_file.relative_to(base)
            try:
                import yaml

                with open(yaml_file, "r", encoding="utf-8") as f:
                    docs = list(yaml.safe_load_all(f))
                if not docs or all(d is None for d in docs):
                    warnings.append(f"YAML file is empty: {rel}")
            except yaml.YAMLError as e:
                errors.append(f"YAML syntax error in {rel}: {e}")
            except ImportError:
                warnings.append(
                    f"PyYAML not installed; skipping YAML validation for {rel}"
                )
                break
            except Exception as e:
                warnings.append(f"Could not parse YAML {rel}: {e}")

    def _check_java_files(
        self,
        base: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Perform basic Java source file checks."""
        for java_file in base.rglob("*.java"):
            rel = java_file.relative_to(base)
            try:
                content = java_file.read_text(encoding="utf-8")
            except Exception as e:
                warnings.append(f"Could not read Java file {rel}: {e}")
                continue

            # Check matching braces
            open_count = content.count("{")
            close_count = content.count("}")
            if open_count != close_count:
                errors.append(
                    f"Mismatched braces in {rel}: "
                    f"{open_count} opening vs {close_count} closing"
                )

            # Check that imports are present
            if "import " not in content and "package " not in content:
                warnings.append(
                    f"Java file {rel} has no import or package statements"
                )

            # Check for common issues
            if "// TODO" in content or "// FIXME" in content:
                warnings.append(f"Java file {rel} contains TODO/FIXME comments")

            # Check for mixed namespaces in a single file
            has_javax = bool(re.search(r"\bjavax\.\w", content))
            has_jakarta = bool(re.search(r"\bjakarta\.\w", content))
            if has_javax and has_jakarta:
                errors.append(
                    f"Mixed javax/jakarta namespaces in {rel}"
                )

    def _check_scripts(
        self,
        base: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Validate shell scripts."""
        for script_file in base.rglob("*.sh"):
            rel = script_file.relative_to(base)
            try:
                content = script_file.read_text(encoding="utf-8")
            except Exception as e:
                warnings.append(f"Could not read script {rel}: {e}")
                continue

            # Check for shebang line
            if not content.startswith("#!/"):
                warnings.append(f"Script {rel} is missing a shebang line")

            # Check for common bash-isms in scripts claiming POSIX
            if "#!/bin/sh" in content:
                bash_isms = [
                    (r"\[\[", "double brackets [[ ]]"),
                    (r"function\s+\w+", "function keyword"),
                    (r"\$\(.*\<.*\)", "process substitution"),
                ]
                for pattern, desc in bash_isms:
                    if re.search(pattern, content):
                        warnings.append(
                            f"Script {rel} uses #!/bin/sh but contains "
                            f"bash-ism: {desc}"
                        )

            # Check that referenced commands exist (basic check)
            if "jboss-cli" in content and "EAP_HOME" not in content:
                warnings.append(
                    f"Script {rel} references jboss-cli but does not "
                    f"reference EAP_HOME"
                )

            # Check for empty scripts
            non_comment_lines = [
                line
                for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            if len(non_comment_lines) < 2:
                warnings.append(f"Script {rel} appears to have minimal content")

    def _check_namespace_consistency(
        self,
        base: Path,
        expected_namespace: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Check javax/jakarta consistency across all Java files."""
        wrong_ns = "jakarta" if expected_namespace == "javax" else "javax"

        for java_file in base.rglob("*.java"):
            rel = java_file.relative_to(base)
            try:
                content = java_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Check imports for wrong namespace (exclude things like
            # javax.crypto which are not part of Jakarta EE)
            ee_packages = [
                "servlet", "ejb", "persistence", "enterprise",
                "inject", "ws", "xml.bind", "json", "mail",
                "transaction", "annotation", "validation",
                "websocket", "security.enterprise",
            ]
            for pkg in ee_packages:
                pattern = rf"\b{re.escape(wrong_ns)}\.{re.escape(pkg)}\b"
                if re.search(pattern, content):
                    errors.append(
                        f"File {rel} uses {wrong_ns}.{pkg} but expected "
                        f"{expected_namespace}.{pkg}"
                    )

    def _check_file_references(
        self,
        base: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Check that files referenced in scripts actually exist."""
        for script_file in base.rglob("*.sh"):
            rel = script_file.relative_to(base)
            try:
                content = script_file.read_text(encoding="utf-8")
            except Exception:
                continue

            # Look for file path references relative to the script dir
            # Pattern: "$SCRIPT_DIR/some/path" or similar
            refs = re.findall(
                r'\$(?:SCRIPT_DIR|DIR|BASE_DIR)["/]([^\s"\']+)',
                content,
            )
            for ref in refs:
                ref_path = base / ref
                # Only warn if it looks like a generated file path (not a variable)
                if "$" not in ref and not ref_path.exists():
                    script_parent = script_file.parent
                    alt_path = script_parent / ref
                    if not alt_path.exists():
                        warnings.append(
                            f"Script {rel} references '{ref}' which does not exist"
                        )

    def _check_essential_files(
        self,
        base: Path,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        """Check that essential reproducer files exist."""
        readme = base / "README.md"
        if not readme.exists():
            warnings.append("Missing README.md")

        # Check for at least one build artifact
        has_pom = any(base.rglob("pom.xml"))
        has_java = any(base.rglob("*.java"))

        if not has_pom:
            warnings.append("No pom.xml found in reproducer")
        if not has_java:
            warnings.append("No Java source files found in reproducer")

    def _detect_namespace(self, base: Path) -> str:
        """Detect which EE namespace the reproducer uses."""
        for java_file in base.rglob("*.java"):
            try:
                content = java_file.read_text(encoding="utf-8")
            except Exception:
                continue
            if re.search(r"\bjakarta\.\w", content):
                return "jakarta"
            if re.search(r"\bjavax\.servlet\b|\bjavax\.ejb\b|\bjavax\.persistence\b", content):
                return "javax"
        return ""
