"""Issue analyzer that uses Claude to classify and strategize reproducer creation."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .llm_client import LLMClient
from .models import CustomerCase, IssueAnalysis


_ANALYSIS_TOOL: dict[str, Any] = {
    "name": "submit_analysis",
    "description": (
        "Submit the structured analysis of the customer issue. "
        "Call this tool exactly once with all fields populated."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "product": {
                "type": "string",
                "description": "Red Hat product name (e.g. EAP, JBoss Web Server, Data Grid).",
            },
            "version": {
                "type": "string",
                "description": "Product version (e.g. 7.4, 8.0).",
            },
            "jdk": {
                "type": "string",
                "description": "JDK version (e.g. OpenJDK 11, OpenJDK 17).",
            },
            "os": {
                "type": "string",
                "description": "Operating system (e.g. RHEL 8, RHEL 9).",
            },
            "deployment_type": {
                "type": "string",
                "description": "Deployment type and server config: standalone, standalone-ha, standalone-full, standalone-full-ha, domain, or openshift.",
            },
            "server_config": {
                "type": "string",
                "description": "EAP server configuration file to use: standalone.xml, standalone-ha.xml, standalone-full.xml, or standalone-full-ha.xml.",
            },
            "num_nodes": {
                "type": "integer",
                "description": "Total number of nodes/instances in the customer topology.",
            },
            "reproducer_nodes": {
                "type": "integer",
                "description": "Minimum number of nodes needed in the reproducer (may be less than customer topology).",
            },
            "application_type": {
                "type": "string",
                "description": "Application type (servlet, EJB, REST, messaging, cache-client, etc.).",
            },
            "subsystem": {
                "type": "string",
                "description": "Primary affected subsystem (undertow, infinispan, ejb3, messaging-activemq, elytron, datasources, jpa, weld, transactions, jgroups, etc.).",
            },
            "trigger": {
                "type": "string",
                "description": "What triggers the issue (specific request, load, timeout, failover, node restart, etc.).",
            },
            "expected_behavior": {
                "type": "string",
                "description": "What the customer expects to happen.",
            },
            "actual_behavior": {
                "type": "string",
                "description": "What actually happens.",
            },
            "error_signature": {
                "type": "string",
                "description": "Key error class or message that identifies this issue.",
            },
            "possible_root_cause": {
                "type": "string",
                "description": "Hypothesis about why this issue occurs.",
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Issue categories. Only include categories directly relevant to the issue. "
                    "Options: classloading, clustering, deployment, configuration, performance, "
                    "security, messaging, database, web, ejb, cdi, jpa, transactions, "
                    "naming, logging, remoting, session, cross-site, cache."
                ),
            },
            "reproducer_strategy": {
                "type": "string",
                "description": (
                    "Detailed strategy for reproducing this issue. Include: "
                    "what app to create, what config to use, what server config file, "
                    "how many nodes, what to trigger."
                ),
            },
            "topology_description": {
                "type": "string",
                "description": "Description of both customer and minimum reproducer topology.",
            },
            "reproduction_requirements": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The concrete conditions and artifacts a FAITHFUL reproduction "
                    "requires that the generated project cannot invent on its own. "
                    "Be honest and specific. Examples: 'exact micro-version "
                    "(e.g. 7.4.14, not GA) because this is a regression', "
                    "'customer heap dump (.hprof)', '3x thread dumps a few seconds apart', "
                    "'GC logs', 'exact JDK vendor+build', 'the customer workload/load profile', "
                    "'mod_cluster/Apache load balancer with sticky sessions', "
                    "'>=3 nodes when the cache has owners=2'. Empty only if the "
                    "generated project is fully self-sufficient."
                ),
            },
            "reproduction_confidence": {
                "type": "string",
                "description": (
                    "How reproducible the issue is from the generated project alone: "
                    "'high' = the generated project reproduces it as-is; "
                    "'partial' = reproduces once the listed artifacts/conditions are supplied; "
                    "'low' = version- or data-specific, cannot be reproduced without the "
                    "exact customer build/data even by a human."
                ),
            },
        },
        "required": [
            "product", "version", "jdk", "os", "deployment_type",
            "server_config", "num_nodes", "reproducer_nodes",
            "application_type", "subsystem", "trigger",
            "expected_behavior", "actual_behavior", "error_signature",
            "possible_root_cause", "categories", "reproducer_strategy",
            "topology_description", "reproduction_requirements",
            "reproduction_confidence",
        ],
        "additionalProperties": False,
    },
}


_SYSTEM_PROMPT = """\
You are an expert Red Hat middleware support engineer and issue analyst.
Your job is to analyze a customer support case and produce a structured
analysis that will guide the creation of a minimal reproducer.

Key analysis rules:
1. Identify the exact Red Hat product and version.
2. Only classify the issue into categories that are DIRECTLY relevant.
   Do NOT include categories just because a keyword appears somewhere.
   A session clustering issue should be categorized as "clustering" and "session",
   NOT as "classloading", "security", "database", etc.
3. Determine the correct EAP server configuration file:
   - standalone.xml: single node, no clustering, no messaging
   - standalone-ha.xml: clustering, session replication, JGroups, failover
   - standalone-full.xml: messaging (JMS, Artemis) without clustering
   - standalone-full-ha.xml: messaging WITH clustering
4. Determine the MINIMUM reproducer topology. If customer has 4 nodes but
   the issue can be reproduced with 2, set reproducer_nodes to 2.
5. For EAP 7.x: javax.* namespace. For EAP 8.x: jakarta.* namespace.
6. Read the ENTIRE case text carefully. Extract node counts, topology,
   and configuration details precisely from what the customer wrote.
7. Identify the PRIMARY subsystem causing the issue, not secondary ones.
   Session replication issue -> infinispan (not undertow).
   HTTP request issue -> undertow. Deployment issue -> ee.
8. Be HONEST about reproducibility. A reproducer that quietly fails to trigger
   the bug is worse than one that clearly states what is still needed. Populate
   reproduction_requirements with the specific conditions/artifacts a faithful
   repro needs (exact micro-version for regressions, heap/thread dumps and JVM
   flags for JVM issues, a load balancer + concurrent load for clustering/session
   failover, etc.) and set reproduction_confidence to high/partial/low honestly.

Call the submit_analysis tool exactly once with your analysis.
"""


class IssueAnalyzer:
    """Analyzes customer cases using an LLM or rule-based fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        self._llm = LLMClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            provider=provider,
        )

    def analyze(self, case: CustomerCase) -> IssueAnalysis:
        if self._llm.available:
            try:
                return self._analyze_with_llm(case)
            except Exception:
                pass
        return self._analyze_rule_based(case)

    def _analyze_with_llm(self, case: CustomerCase) -> IssueAnalysis:
        user_message = self._build_analysis_prompt(case)
        data = self._llm.analyze_with_tool(
            user_message, _SYSTEM_PROMPT, _ANALYSIS_TOOL, "submit_analysis",
        )
        if data:
            return self._dict_to_analysis(data)
        raise ValueError("LLM did not return structured analysis")

    def _build_analysis_prompt(self, case: CustomerCase) -> str:
        parts: list[str] = ["Please analyze the following customer support case:\n"]

        field_map = {
            "Product": case.product,
            "Version": case.version,
            "JDK": case.jdk,
            "OS": case.os,
            "OpenShift Version": case.openshift_version,
            "Deployment Type": case.deployment_type,
            "Number of Nodes": str(case.num_nodes) if case.num_nodes > 1 else "",
            "Application Type": case.application_type,
            "Subsystem": case.subsystem,
            "JVM Options": case.jvm_options,
        }

        for label, value in field_map.items():
            if value:
                parts.append(f"{label}: {value}")

        section_map = {
            "Description": case.description,
            "Topology": case.topology,
            "Error Messages": case.error_messages,
            "Stack Trace": case.stack_traces,
            "Reproduction Steps": case.reproduction_steps,
            "Expected Behavior": case.expected_behavior,
            "Actual Behavior": case.actual_behavior,
        }

        for label, value in section_map.items():
            if value:
                parts.append(f"\n## {label}\n{value}")

        if case.config_files:
            parts.append("\n## Configuration Files")
            for filename, content in case.config_files.items():
                parts.append(f"\n### {filename}\n```\n{content}\n```")

        return "\n".join(parts)

    def _dict_to_analysis(self, data: dict) -> IssueAnalysis:
        return IssueAnalysis(
            product=data.get("product", ""),
            version=data.get("version", ""),
            jdk=data.get("jdk", ""),
            os=data.get("os", ""),
            deployment_type=data.get("deployment_type", ""),
            num_nodes=data.get("num_nodes", 1),
            application_type=data.get("application_type", ""),
            subsystem=data.get("subsystem", ""),
            trigger=data.get("trigger", ""),
            expected_behavior=data.get("expected_behavior", ""),
            actual_behavior=data.get("actual_behavior", ""),
            error_signature=data.get("error_signature", ""),
            possible_root_cause=data.get("possible_root_cause", ""),
            categories=data.get("categories", []),
            reproducer_strategy=data.get("reproducer_strategy", ""),
            topology_description=data.get("topology_description", ""),
            reproduction_requirements=data.get("reproduction_requirements", []),
            reproduction_confidence=data.get("reproduction_confidence", ""),
        )

    # ------------------------------------------------------------------
    # Rule-based fallback — thorough heuristic analysis
    # ------------------------------------------------------------------

    def _analyze_rule_based(self, case: CustomerCase) -> IssueAnalysis:
        """Perform heuristic analysis without Claude."""
        all_text = "\n".join([
            case.description, case.error_messages, case.stack_traces,
            case.reproduction_steps, case.expected_behavior,
            case.actual_behavior, case.deployment_type, case.topology,
        ]).lower()

        # --- Determine deployment type and server config ---
        deployment_type = case.deployment_type or "standalone"
        server_config = self._determine_server_config(all_text, deployment_type)

        # Use num_nodes already parsed by the improved CaseParser
        num_nodes = case.num_nodes

        # --- Determine primary subsystem ---
        subsystem = case.subsystem or self._infer_subsystem(all_text)

        # --- Determine categories (PRECISE, not over-broad) ---
        categories = self._infer_categories(all_text, subsystem)

        # --- Build analysis ---
        analysis = IssueAnalysis(
            product=case.product or self._infer_product(all_text),
            version=case.version,
            jdk=case.jdk,
            os=case.os,
            deployment_type=deployment_type,
            num_nodes=num_nodes,
            application_type=case.application_type or self._infer_app_type(all_text, subsystem),
            subsystem=subsystem,
            expected_behavior=case.expected_behavior,
            actual_behavior=case.actual_behavior,
            categories=categories,
            error_signature=self._extract_error_signature(case),
            trigger=self._infer_trigger(all_text),
            possible_root_cause=self._infer_root_cause(all_text, subsystem, categories),
        )

        analysis.reproducer_strategy = self._build_strategy(analysis, server_config)
        analysis.topology_description = self._build_topology(analysis)
        analysis.reproduction_requirements, analysis.reproduction_confidence = (
            self._infer_reproduction_requirements(analysis, all_text)
        )

        return analysis

    def _infer_reproduction_requirements(
        self, analysis: IssueAnalysis, all_text: str,
    ) -> tuple[list[str], str]:
        """Derive an honest list of what a faithful repro needs, plus a confidence.

        Confidence: 'high' fully self-contained, 'partial' needs listed artifacts,
        'low' version/data specific (not reproducible without the exact build/data).
        """
        reqs: list[str] = []
        confidence = "high"
        cats = analysis.categories

        # Version regression -> needs the EXACT micro-version.
        if any(kw in all_text for kw in [
            "after upgrade", "after upgrading", "since upgrading", "regression",
            "started after", "worked in", "used to work", "downgrade",
        ]):
            reqs.append(
                "Exact micro-version reported by the customer (e.g. a specific CP "
                "like 7.4.14, not just the GA build) -- this reads as a version "
                "regression and may not reproduce on any other build."
            )
            confidence = "low"

        # JVM / performance issues -> need runtime artifacts.
        if any(c in cats for c in ["performance"]) or any(kw in all_text for kw in [
            "outofmemory", "oom", "memory leak", "heap", "metaspace",
            "gc overhead", "high cpu", "thread leak", "garbage collect",
        ]):
            reqs += [
                "Exact JDK vendor + build (e.g. 'OpenJDK 17.0.9 Temurin')",
                "Full JVM command line / flags (-Xms/-Xmx, -XX:MaxMetaspaceSize, GC algorithm)",
                "Heap dump (.hprof) captured at failure (enable -XX:+HeapDumpOnOutOfMemoryError)",
                "Three thread dumps taken a few seconds apart (jstack)",
                "GC logs (-Xlog:gc*)",
                "The customer workload / load profile that triggers the growth",
            ]
            if confidence != "low":
                confidence = "partial"

        # Clustering / session / load balancing -> the generated project now bundles
        # the load balancer and multi-node topology, but timing is intermittent.
        if any(c in cats for c in ["clustering", "session", "cross-site"]):
            reqs += [
                "mod_cluster / Apache load balancer with sticky sessions "
                "(bundled by this reproducer)",
                ">=3 nodes when the web cache uses owners=2 so sessions are not on "
                "every node (bundled by this reproducer)",
                "Concurrent load applied during the failover window "
                "(bundled by this reproducer's test)",
            ]
            if confidence == "high":
                confidence = "partial"

        return reqs, confidence

    def _determine_server_config(self, text: str, deployment_type: str) -> str:
        dt = deployment_type.lower()
        needs_ha = any(kw in text for kw in [
            "standalone-ha", "standalone_ha", "ha.xml", "full-ha",
            "cluster", "jgroups", "failover", "session replicat",
            "distributed cache", "replicated cache", "mod_cluster",
            "cross-site", "backup site", "infinispan",
        ]) or "ha" in dt

        needs_messaging = any(kw in text for kw in [
            "standalone-full", "jms", "messaging", "activemq", "artemis",
            "queue", "topic", "mdb", "message-driven",
        ])

        if needs_messaging and needs_ha:
            return "standalone-full-ha.xml"
        if needs_messaging:
            return "standalone-full.xml"
        if needs_ha:
            return "standalone-ha.xml"
        return "standalone.xml"

    def _infer_product(self, text: str) -> str:
        if any(kw in text for kw in ["eap", "jboss eap", "wildfly"]):
            return "EAP"
        if any(kw in text for kw in ["datagrid", "data grid", "infinispan server", "jdg"]):
            return "Data Grid"
        if any(kw in text for kw in ["jws", "jboss web server", "tomcat"]):
            return "JBoss Web Server"
        if any(kw in text for kw in ["amq", "activemq broker"]):
            return "AMQ"
        if any(kw in text for kw in ["sso", "keycloak"]):
            return "Red Hat SSO"
        return "EAP"

    def _infer_subsystem(self, text: str) -> str:
        """Infer the PRIMARY affected subsystem. Order matters — more specific first."""
        checks = [
            ("infinispan", ["session replicat", "session failover", "session loss",
                            "cache replicat", "distributed cache", "replicated cache",
                            "cross-site", "infinispan", "ispn", "cache container"]),
            ("jgroups", ["jgroups", "cluster view", "merge", "split brain",
                         "relay2", "mping", "tcpping", "fd_sock", "nakack"]),
            ("elytron", ["elytron", "security-domain", "security domain",
                         "sasl", "credential", "keystore", "truststore",
                         "authentication", "authorization"]),
            ("messaging-activemq", ["jms", "messaging", "activemq", "artemis",
                                     "queue", "topic", "mdb", "message-driven"]),
            ("datasources", ["datasource", "jdbc", "connection pool", "xa-datasource",
                              "database", "pool size", "connection leak"]),
            ("ejb3", ["ejb", "stateless", "stateful", "singleton bean",
                      "remote ejb", "ejb client", "timer"]),
            ("weld", ["cdi", "weld", "inject", "@inject", "bean manager"]),
            ("transactions", ["transaction timeout", "xa recovery", "2pc",
                               "two-phase commit", "arjuna", "narayana"]),
            ("jpa", ["jpa", "hibernate", "entitymanager", "persistence unit",
                      "lazy loading", "n+1"]),
            ("undertow", ["undertow", "http listener", "https listener",
                           "ajp", "reverse proxy", "mod_cluster",
                           "request timeout", "max-connections"]),
            ("ee", ["classnotfound", "noclassdef", "classloading",
                     "jboss-deployment-structure", "module dependency",
                     "deployment failed", "javax.*jakarta"]),
            ("logging", ["logging subsystem", "log handler", "custom handler"]),
        ]

        for subsystem, keywords in checks:
            if any(kw in text for kw in keywords):
                return subsystem

        return "undertow"

    def _infer_categories(self, text: str, subsystem: str) -> list[str]:
        """Infer ONLY directly relevant categories. Precision over recall."""
        categories: list[str] = []

        category_checks = {
            "clustering": ["cluster", "jgroups", "failover", "session replicat",
                           "distributed cache", "node join", "node leave",
                           "split brain", "merge", "mod_cluster"],
            "session": ["session", "httpsession", "session id", "session invalid",
                        "session loss", "session timeout", "distributable"],
            "cross-site": ["cross-site", "cross site", "backup site", "relay2",
                           "xsite", "site replicat"],
            "cache": ["cache", "infinispan", "hotrod", "hot rod", "ispn"],
            "deployment": ["deployment failed", "deployment error", "failed to deploy",
                           "undeploy", "deploy error", "msc000001"],
            "classloading": ["classnotfound", "noclassdef", "linkage error",
                              "classloading", "module dependency",
                              "jboss-deployment-structure"],
            "security": ["authentication fail", "authorization fail",
                          "ssl error", "tls error", "handshake fail",
                          "certificate", "keystore", "truststore",
                          "sasl", "elytron", "security domain"],
            "messaging": ["jms", "queue", "topic", "messaging",
                           "activemq", "artemis", "mdb"],
            "database": ["jdbc", "datasource", "connection pool",
                          "sql exception", "database"],
            "web": ["http 500", "http 503", "servlet error",
                     "undertow error", "ut00", "request processing error"],
            "performance": ["memory leak", "outofmemory", "oom",
                             "high cpu", "thread leak", "gc overhead",
                             "heap exhausted", "metaspace"],
            "ejb": ["ejb error", "ejb fail", "stateless bean", "stateful bean",
                    "remote ejb", "ejb invocation", "ejb3"],
            "transactions": ["transaction timeout", "xa recovery", "rollback exception",
                              "2pc fail", "arjuna", "narayana"],
            "configuration": ["configuration error", "misconfigur",
                                "invalid config", "xml parse error"],
        }

        for cat, keywords in category_checks.items():
            if any(kw in text for kw in keywords):
                categories.append(cat)

        # Add category based on primary subsystem if not already present
        subsystem_category_map = {
            "infinispan": "cache",
            "jgroups": "clustering",
            "elytron": "security",
            "messaging-activemq": "messaging",
            "datasources": "database",
            "ejb3": "ejb",
            "weld": "cdi",
            "transactions": "transactions",
            "undertow": "web",
            "ee": "deployment",
            "jpa": "database",
        }
        mapped = subsystem_category_map.get(subsystem)
        if mapped and mapped not in categories:
            categories.append(mapped)

        return categories or ["configuration"]

    def _infer_app_type(self, text: str, subsystem: str) -> str:
        if any(kw in text for kw in ["rest", "jax-rs", "jaxrs", "@path"]):
            return "REST"
        if any(kw in text for kw in ["ejb", "stateless", "stateful"]):
            return "EJB"
        if any(kw in text for kw in ["jms", "mdb", "message-driven"]):
            return "messaging"
        if any(kw in text for kw in ["hotrod", "hot rod", "cache client"]):
            return "cache-client"
        if subsystem in ("infinispan", "jgroups"):
            return "session-servlet"
        return "servlet"

    def _extract_error_signature(self, case: CustomerCase) -> str:
        text = case.error_messages + "\n" + case.stack_traces

        # Java exception with message
        exc_match = re.search(
            r"([\w.]+(?:Exception|Error)):\s*(.{0,80})",
            text,
        )
        if exc_match:
            return f"{exc_match.group(1)}: {exc_match.group(2).strip()}"

        # Just exception class
        exc_match = re.search(
            r"(?:Caused by:\s*|Exception:\s*)?([\w.]+(?:Exception|Error))",
            text,
        )
        if exc_match:
            return exc_match.group(1)

        # WFLY/JBAS/ISPN error codes
        code_match = re.search(r"((?:WFLY|JBAS|WELD|ARJUNA|ISPN|JBTHR)\d+)", text)
        if code_match:
            return code_match.group(1)

        for line in case.error_messages.splitlines():
            line = line.strip()
            if line and ("error" in line.lower() or "warn" in line.lower()):
                return line[:150]

        return ""

    def _infer_trigger(self, text: str) -> str:
        triggers = [
            (["under load", "concurrent request", "high traffic", "load test"], "concurrent load"),
            (["failover", "node down", "node fail", "node crash"], "node failover"),
            (["restart", "node restart", "server restart"], "server/node restart"),
            (["deploy", "redeploy", "undeploy"], "application deployment"),
            (["timeout", "request timeout", "connection timeout"], "timeout"),
            (["cross-site", "site fail", "wan"], "cross-site operation"),
            (["login", "authenticat", "credential"], "authentication attempt"),
            (["ssl", "tls", "handshake", "certificate"], "TLS handshake"),
            (["startup", "boot", "server start"], "server startup"),
        ]
        for keywords, trigger in triggers:
            if any(kw in text for kw in keywords):
                return trigger
        return "specific request or operation"

    def _infer_root_cause(self, text: str, subsystem: str, categories: list[str]) -> str:
        if "session" in categories and "clustering" in categories:
            return "Session replication or failover issue in clustered environment"
        if "cross-site" in categories:
            return "Cross-site replication connectivity or configuration issue"
        if "classloading" in categories:
            return "Class visibility or module dependency issue"
        if "security" in categories:
            return "Security configuration or credential issue"
        if "database" in categories:
            return "Database connectivity or connection pool issue"
        if "performance" in categories:
            return "Resource exhaustion or contention issue"
        return f"Configuration or behavior issue in {subsystem} subsystem"

    def _build_strategy(self, analysis: IssueAnalysis, server_config: str) -> str:
        parts: list[str] = []
        parts.append(
            f"Create a minimal {analysis.application_type} application "
            f"targeting the {analysis.subsystem} subsystem."
        )
        parts.append(f"Use server configuration: {server_config}.")

        if analysis.num_nodes > 1:
            reproducer_nodes = min(analysis.num_nodes, 2)
            if "cross-site" in analysis.categories:
                reproducer_nodes = min(analysis.num_nodes, 4)
            parts.append(
                f"Customer has {analysis.num_nodes} nodes. "
                f"Reproducer needs minimum {reproducer_nodes} nodes to demonstrate the issue."
            )
        if analysis.error_signature:
            parts.append(f"Target error: {analysis.error_signature}.")
        return " ".join(parts)

    def _build_topology(self, analysis: IssueAnalysis) -> str:
        if analysis.num_nodes <= 1:
            return f"Single {analysis.deployment_type} instance"

        reproducer_nodes = min(analysis.num_nodes, 2)
        if "cross-site" in analysis.categories:
            reproducer_nodes = min(analysis.num_nodes, 4)

        parts = [
            f"Customer: {analysis.num_nodes}-node {analysis.deployment_type} environment.",
        ]
        if reproducer_nodes < analysis.num_nodes:
            parts.append(
                f"Reproducer: {reproducer_nodes}-node cluster "
                f"(minimum to reproduce the issue)."
            )
        else:
            parts.append(
                f"Reproducer: {reproducer_nodes}-node cluster."
            )
        return " ".join(parts)
