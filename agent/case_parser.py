"""Parser for customer support case files into structured CustomerCase objects."""

from __future__ import annotations

import re
from pathlib import Path

from .models import CustomerCase


class CaseParser:
    """Parses customer case descriptions from files or text into structured data."""

    FIELD_LABELS: dict[str, list[str]] = {
        "product": ["product", "component", "red hat product", "rh product"],
        "version": ["version", "product version", "eap version", "jboss version",
                     "datagrid version", "dg version", "jws version"],
        "jdk": ["jdk", "java version", "java", "jdk version", "openjdk"],
        "os": ["os", "operating system", "rhel", "platform", "os version"],
        "openshift_version": [
            "openshift version", "ocp version", "openshift", "ocp",
        ],
        "deployment_type": [
            "deployment type", "deployment", "deploy type", "deployment mode",
            "server mode",
        ],
        "application_type": [
            "application type", "app type", "application", "app",
        ],
        "subsystem": ["subsystem", "affected subsystem", "area"],
        "jvm_options": ["jvm options", "jvm args", "java options", "java opts",
                        "jvm parameters"],
    }

    SECTION_LABELS: dict[str, list[str]] = {
        "description": [
            "description", "issue description", "problem description",
            "summary", "issue", "problem", "details",
        ],
        "error_messages": [
            "error messages", "error message", "error", "errors", "error output",
            "log output", "log messages",
        ],
        "stack_traces": [
            "stack trace", "stacktrace", "stack traces", "traceback",
            "exception", "exception trace", "full stack trace",
        ],
        "reproduction_steps": [
            "reproduction steps", "steps to reproduce", "repro steps",
            "how to reproduce", "steps", "reproduce",
            "reproduction steps (from customer)",
        ],
        "expected_behavior": [
            "expected behavior", "expected result", "expected",
            "expected outcome", "what should happen",
        ],
        "actual_behavior": [
            "actual behavior", "actual result", "actual",
            "actual outcome", "observed behavior", "what happens",
        ],
        "topology": [
            "topology", "cluster topology", "deployment topology",
            "environment topology", "architecture",
        ],
        "configuration": [
            "configuration", "config", "environment details",
            "environment", "setup",
        ],
    }

    def parse_file(self, filepath: str) -> CustomerCase:
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Case file not found: {filepath}")
        text = path.read_text(encoding="utf-8")
        return self.parse_text(text)

    def parse_text(self, text: str) -> CustomerCase:
        case = CustomerCase()

        # Extract single-line fields
        case.product = self._extract_field(text, self.FIELD_LABELS["product"])
        case.version = self._extract_field(text, self.FIELD_LABELS["version"])
        case.jdk = self._extract_field(text, self.FIELD_LABELS["jdk"])
        case.os = self._extract_field(text, self.FIELD_LABELS["os"])
        case.openshift_version = self._extract_field(
            text, self.FIELD_LABELS["openshift_version"]
        )
        case.deployment_type = self._extract_field(
            text, self.FIELD_LABELS["deployment_type"]
        )
        case.application_type = self._extract_field(
            text, self.FIELD_LABELS["application_type"]
        )
        case.subsystem = self._extract_field(text, self.FIELD_LABELS["subsystem"])
        case.jvm_options = self._extract_field(text, self.FIELD_LABELS["jvm_options"])

        # Extract multi-line sections
        case.description = self._extract_section(
            text, self.SECTION_LABELS["description"]
        )
        case.error_messages = self._extract_section(
            text, self.SECTION_LABELS["error_messages"]
        )
        case.stack_traces = self._extract_section(
            text, self.SECTION_LABELS["stack_traces"]
        )
        case.reproduction_steps = self._extract_section(
            text, self.SECTION_LABELS["reproduction_steps"]
        )
        case.expected_behavior = self._extract_section(
            text, self.SECTION_LABELS["expected_behavior"]
        )
        case.actual_behavior = self._extract_section(
            text, self.SECTION_LABELS["actual_behavior"]
        )
        case.topology = self._extract_section(
            text, self.SECTION_LABELS["topology"]
        )

        # Extract configuration section (bullet-point style)
        config_section = self._extract_section(
            text, self.SECTION_LABELS["configuration"]
        )
        if config_section:
            case.topology = (case.topology + "\n" + config_section).strip()

        # Extract number of nodes from multiple sources
        case.num_nodes = self._extract_num_nodes(text, case)

        # Infer version from product field if version is empty
        if not case.version and case.product:
            ver_match = re.search(r"(\d+\.\d+[\.\d]*)", case.product)
            if ver_match:
                case.version = ver_match.group(1)
                case.product = re.sub(r"\s*\d+\.\d+[\.\d]*\s*", " ", case.product).strip()

        # Extract config file blocks
        case.config_files = self._extract_config_files(text)

        # Fallback: if no structured description, use entire text
        if not case.description:
            case.description = text.strip()

        return case

    def _extract_num_nodes(self, text: str, case: CustomerCase) -> int:
        """Extract node count from all available sources in the case text."""
        text_lower = text.lower()

        # 1. Try explicit "Nodes:" or "Number of nodes:" field
        num_str = self._extract_field(text, [
            "num nodes", "number of nodes", "nodes", "cluster size",
            "number of servers", "replicas",
        ])
        if num_str:
            digits = re.sub(r"[^\d]", "", num_str)
            if digits:
                return int(digits)

        # 2. Scan the FULL text for node/server count patterns.
        #    Be precise: "2 owners", "7.4 nodes" (version!), "2 connections" must NOT match.
        #    Use (?<!\.\d) and (?<!\d\.) to avoid matching digits inside version numbers.
        node_patterns = [
            r"(?<!\.)(\d+)\s+(?:eap|jboss|datagrid|data\s*grid|dg|jws)\s+(?:nodes?|instances?|servers?)",
            r"(?<!\.\d)(?<!\d\.)(?:^|[\s\-\(])(\d+)\s+(?:nodes?|servers?|pods?|instances?)\s+(?:in\s+(?:standalone|domain|cluster|the)|behind\s+|running|deployed|configured)",
            r"(\d+)[\s-]*node\s+cluster",
            r"cluster\s+(?:of|with)\s+(\d+)\s*(?:nodes?|members?|servers?)",
        ]
        counts = []
        for pattern in node_patterns:
            for m in re.finditer(pattern, text_lower):
                val = int(m.group(1))
                if 1 < val <= 100:
                    counts.append(val)

        # 3. Count explicit node references like "node1", "node2" ONLY in
        #    topology/configuration sections, NOT in log output or stack traces.
        #    Skip this heuristic — it's too noisy with log snippets.

        # 4. Look in deployment_type for node count (avoid version numbers like "7.4")
        if case.deployment_type:
            dt_match = re.search(r"(?<!\.)(?<!\d)(\d+)\s*node", case.deployment_type.lower())
            if dt_match:
                val = int(dt_match.group(1))
                if 1 < val <= 100:
                    counts.append(val)

        # 5. Look in topology section
        if case.topology:
            topo_match = re.search(r"(?<!\.)(?<!\d)(\d+)\s*(?:nodes?|servers?|pods?|instances?|members?)", case.topology.lower())
            if topo_match:
                val = int(topo_match.group(1))
                if val > 1:
                    counts.append(val)

        # Return the maximum count found (total nodes in the environment)
        if counts:
            return max(counts)

        # 6. Infer from clustering keywords
        if any(kw in text_lower for kw in [
            "cluster", "failover", "replicat", "ha ", "high availability",
            "jgroups", "cross-site", "backup site", "session sharing",
        ]):
            return 2

        return 1

    def _extract_field(self, text: str, field_names: list[str]) -> str:
        for label in sorted(field_names, key=len, reverse=True):
            pattern = rf"(?:^|\n)\s*{re.escape(label)}\s*[:=]\s*(.+?)(?:\n|$)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _extract_section(self, text: str, section_names: list[str]) -> str:
        for name in sorted(section_names, key=len, reverse=True):
            # Match section headers: "Section Name:", "## Section Name", etc.
            # Content runs until the next section header or end of text.
            pattern = (
                rf"(?:^|\n)\s*(?:#{{1,3}}\s*)?{re.escape(name)}\s*:?\s*\n"
                rf"(.*?)(?=\n\s*(?:#{{1,3}}\s*)?(?:[A-Z][\w\s/()]{{2,}})\s*(?:\([^)]*\)\s*)?:\s*\n|\Z)"
            )
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match and match.group(1):
                content = match.group(1).strip()
                if content:
                    return content
        return ""

    def _extract_config_files(self, text: str) -> dict[str, str]:
        configs: dict[str, str] = {}

        fenced_pattern = r"```\w*\s*(?:<!---?\s*([\w./\-]+)\s*-?-->)?\s*\n(.*?)```"
        for match in re.finditer(fenced_pattern, text, re.DOTALL):
            filename = match.group(1)
            content = match.group(2).strip()
            if filename:
                configs[filename] = content

        config_names = [
            "standalone.xml", "standalone-full.xml", "standalone-ha.xml",
            "standalone-full-ha.xml", "domain.xml", "host.xml", "pom.xml",
            "application.properties", "application.yml", "jboss-web.xml",
            "web.xml", "persistence.xml", "jboss-deployment-structure.xml",
            "infinispan.xml", "server.xml", "httpd.conf",
        ]
        for name in config_names:
            pattern = (
                rf"(?:^|\n)\s*{re.escape(name)}\s*:\s*\n"
                rf"(.*?)(?=\n\s*\w[\w.\-]*\.\w+\s*:\s*\n|\Z)"
            )
            match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
            if match:
                content = match.group(1).strip()
                if content and name not in configs:
                    configs[name] = content

        return configs
