"""Data models for the Red Hat Support Reproducer Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CustomerCase:
    """Structured representation of a customer support case."""

    product: str = ""
    version: str = ""
    jdk: str = ""
    os: str = ""
    openshift_version: str = ""
    topology: str = ""
    description: str = ""
    error_messages: str = ""
    stack_traces: str = ""
    config_files: dict[str, str] = field(default_factory=dict)
    reproduction_steps: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    deployment_type: str = ""
    num_nodes: int = 1
    application_type: str = ""
    subsystem: str = ""
    jvm_options: str = ""


@dataclass
class IssueAnalysis:
    """Analysis results from Claude or rule-based classification."""

    product: str = ""
    version: str = ""
    jdk: str = ""
    os: str = ""
    deployment_type: str = ""
    num_nodes: int = 1
    application_type: str = ""
    subsystem: str = ""
    trigger: str = ""
    expected_behavior: str = ""
    actual_behavior: str = ""
    error_signature: str = ""
    possible_root_cause: str = ""
    categories: list[str] = field(default_factory=list)
    reproducer_strategy: str = ""
    topology_description: str = ""
    reproduction_requirements: list[str] = field(default_factory=list)
    reproduction_confidence: str = ""


@dataclass
class ReproducerConfig:
    """Configuration for reproducer generation."""

    output_dir: str = "./reproducer-output"
    issue_analysis: Optional[IssueAnalysis] = None
    local_reproducer: bool = True
    openshift_reproducer: bool = True
    eap7_app: bool = False
    eap8_app: bool = False
    datagrid: bool = False
    jws: bool = False
