"""Ingest a directory of raw customer artifacts into a single case description.

Support cases do not arrive as a tidy paragraph. They arrive as a pile: a
server.log with 400k lines, four standalone-ha.xml variants, an httpd error_log,
three thread dumps taken a minute apart and a 2 GB heap dump. This module turns
that pile into text the analyzer can reason about, without blowing up the
context window and without leaking customer credentials.

Expected layout (everything except case.txt is optional):

    <case-dir>/
        case.txt          the scenario, in the format the CLI already accepts
        configs/          standalone-ha.xml, httpd.conf, infinispan.xml, ...
        logs/             server.log, boot.log, error_log, access_log, gc.log
        dumps/            threaddump-*.txt, *.hprof
        attachments/      anything else worth keeping

Files dropped loose in <case-dir> are classified by name and extension, so a
flat directory works too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# A log is distilled, never included whole: keep this many lines of each kind.
_MAX_ERROR_LINES = 60
_MAX_TRACES = 6
_TRACE_FRAMES = 12
_MAX_CONFIG_CHARS = 60_000
_BINARY_SNIFF = 8192


@dataclass
class BundleResult:
    """What an ingested case directory yields."""

    text: str = ""                                    # synthesized case description
    config_files: dict[str, str] = field(default_factory=dict)
    manifest: list[str] = field(default_factory=list)  # human-readable "what I read"
    warnings: list[str] = field(default_factory=list)
    detected_product: str = ""
    detected_version: str = ""


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

_CONFIG_SUFFIXES = {".xml", ".conf", ".properties", ".yaml", ".yml", ".cli", ".json"}
_LOG_SUFFIXES = {".log", ".out", ".txt"}
_HEAP_SUFFIXES = {".hprof", ".bin", ".phd"}
_ARCHIVE_SUFFIXES = {".zip", ".gz", ".tgz", ".bz2", ".xz", ".tar", ".jar", ".war", ".ear"}

_THREAD_DUMP_HINTS = ("threaddump", "thread-dump", "thread_dump", "jstack", "tdump")
_HEAP_DUMP_HINTS = ("heapdump", "heap-dump", "heap_dump", "hprof")
_GC_HINTS = ("gc.log", "gclog", "gc-")


def _classify(path: Path, root: Path) -> str:
    """Return one of: config, serverlog, accesslog, gclog, threaddump, heapdump,
    archive, other."""
    name = path.name.lower()
    suffix = path.suffix.lower()
    # The directory the user filed it under wins when it is unambiguous.
    parent = path.parent.name.lower() if path.parent != root else ""

    if suffix in _HEAP_SUFFIXES or any(h in name for h in _HEAP_DUMP_HINTS):
        return "heapdump"
    if any(h in name for h in _THREAD_DUMP_HINTS):
        return "threaddump"
    if any(h in name for h in _GC_HINTS):
        return "gclog"
    if suffix in _ARCHIVE_SUFFIXES:
        return "archive"
    if suffix in _CONFIG_SUFFIXES and parent != "logs":
        return "config"
    if parent == "configs":
        return "config"
    if "access" in name:
        return "accesslog"
    if suffix in _LOG_SUFFIXES or parent == "logs":
        return "serverlog"
    return "other"


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return b"\0" in fh.read(_BINARY_SNIFF)
    except OSError:
        return True


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _human_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num/1.0:.1f}{unit}"
        num /= 1024.0
    return f"{num}B"


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

# Credentials must never reach the LLM or the generated package. Hostnames are
# deliberately NOT redacted: topology inference needs them, and they are not
# secrets. Anything matched here is replaced in place so line numbers survive.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r'((?:password|passwd|pwd|secret|credential)\s*[:=]\s*)("?)([^"\s,;]+)',
                re.IGNORECASE), r'\1\2***REDACTED***'),
    (re.compile(r'(\bvalue\s*=\s*")([^"]*)(")\s*(?=.*?(?:password|secret))',
                re.IGNORECASE), r'\1***REDACTED***\3'),
    (re.compile(r'((?:api[_-]?key|token|bearer|authorization)\s*[:=]\s*)(\S+)',
                re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
                re.DOTALL), '***REDACTED PRIVATE KEY***'),
    (re.compile(r'\b[\w.+-]+@[\w-]+\.[\w.]+\b'), '***REDACTED EMAIL***'),
]


def redact(text: str) -> tuple[str, int]:
    """Strip credentials. Returns the cleaned text and how many hits were made."""
    hits = 0
    for pattern, replacement in _REDACTIONS:
        text, n = pattern.subn(replacement, text)
        hits += n
    return text, hits


# --------------------------------------------------------------------------
# Product / version detection from the customer's own logs
# --------------------------------------------------------------------------

_BANNERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"JBoss EAP\s+([0-9][0-9.]*(?:\.GA|\.CR\d+)?)", re.IGNORECASE), "JBoss EAP"),
    (re.compile(r"Red Hat Data Grid\s+([0-9][0-9.]*)", re.IGNORECASE), "Red Hat Data Grid"),
    (re.compile(r"Infinispan.*?\bversion\s+([0-9][0-9.]*)", re.IGNORECASE), "Red Hat Data Grid"),
    (re.compile(r"JBoss Web Server\s+([0-9][0-9.]*)", re.IGNORECASE), "JBoss Web Server"),
    (re.compile(r"Apache Tomcat/([0-9][0-9.]*)", re.IGNORECASE), "JBoss Web Server"),
    (re.compile(r"Apache/([0-9][0-9.]*)", re.IGNORECASE), "Apache httpd"),
    (re.compile(r"WildFly\s+(?:Full|Core)\s+([0-9][0-9.]*)", re.IGNORECASE), "WildFly"),
]


def _detect_product(text: str) -> tuple[str, str]:
    for pattern, product in _BANNERS:
        m = pattern.search(text)
        if m:
            return product, m.group(1)
    return "", ""


# --------------------------------------------------------------------------
# Log distillation
# --------------------------------------------------------------------------

_CODE_RE = re.compile(r"\b(WFLY[A-Z]{0,4}\d{4,}|JBAS\d{6}|ISPN\d{6}|JGRP\d{6}|"
                      r"UT\d{6}|AH\d{5}|IJ\d{6})\b")
_SEVERITY_RE = re.compile(r"\b(FATAL|SEVERE|ERROR|WARN|INFO|DEBUG|TRACE)\b")
_LOUD = {"FATAL", "SEVERE", "ERROR", "WARN"}

# Codes worth keeping even at INFO, because they are the milestones you check
# first: did it boot, did it die, who is in the cluster, what deployed.
_NOTABLE_INFO_CODES = {
    "WFLYSRV0025",   # started (and how long it took)
    "WFLYSRV0026",   # started with errors
    "WFLYSRV0049",   # version banner
    "WFLYSRV0050",   # stopped
    "WFLYSRV0056",   # unrecoverable boot failure
    "WFLYSRV0021",   # deployment failed
    "WFLYSRV0010",   # deployed
    "WFLYUT0021",    # registered web context
    "ISPN000094",    # received new cluster view
    "ISPN000093",    # topology change
    "ISPN000310",    # starting rebalance
    "ISPN000336",    # rebalance finished
    "WFLYCLINF0002", # started cache
}

# Console logs are full of colour escapes; they defeat de-duplication.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\[\d{1,2}m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
_EXCEPTION_RE = re.compile(
    r"^(?:.*?\s)?((?:[\w$]+\.)+[\w$]*(?:Exception|Error|Throwable)(?::.*)?)$")
_FRAME_RE = re.compile(r"^\s+(?:at\s|\.\.\.\s|Caused by:|Suppressed:)")
# A leading timestamp makes an otherwise unremarkable line a new log record.
_TIMESTAMP_RE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}|\[\w{3}\s)")


def distill_log(text: str, source: str) -> tuple[str, dict[str, int]]:
    """Reduce a server log to the lines that carry diagnostic signal.

    Keeps: severity lines, product error codes, and exception traces (header
    plus a bounded number of frames). Identical messages are collapsed with a
    count, because 40,000 repetitions of one WARN is one fact, not 40,000.
    """
    lines = _strip_ansi(text).splitlines()
    stats = {"lines": len(lines), "errors": 0, "warnings": 0, "traces": 0}

    seen: dict[str, int] = {}
    order: list[str] = []
    traces: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()
        sev = _SEVERITY_RE.search(line)
        code = _CODE_RE.search(line)
        exc = _EXCEPTION_RE.match(line)

        level = sev.group(1) if sev else ""
        loud = level in _LOUD
        if loud:
            if level == "WARN":
                stats["warnings"] += 1
            else:
                stats["errors"] += 1

        # An INFO line is only interesting if it carries a milestone code;
        # otherwise a normal boot buries the one ERROR that matters.
        notable = bool(code) and (loud or not level
                                  or code.group(1) in _NOTABLE_INFO_CODES)
        if loud or notable:
            # Collapse on the message with volatile parts (timestamps, ids,
            # thread names, numbers) masked out.
            key = re.sub(r"\d+", "#", line)
            key = re.sub(r"\s+", " ", key)[:400]
            if key not in seen:
                seen[key] = 0
                order.append(line.strip())
            seen[key] += 1

        if exc and len(traces) < _MAX_TRACES:
            frames = [line.strip()]
            j = i + 1
            while (j < len(lines) and len(frames) <= _TRACE_FRAMES
                   and _FRAME_RE.match(lines[j])):
                frames.append(lines[j].strip())
                j += 1
            if len(frames) > 1:            # a header with no frames is just a message
                traces.append("\n".join(frames))
                stats["traces"] += 1
                i = j
                continue
        i += 1

    out: list[str] = []
    if order:
        out.append(f"-- distinct ERROR/WARN/code lines from {source} "
                   f"({len(order)} of {stats['lines']} total lines) --")
        for line in order[:_MAX_ERROR_LINES]:
            key = re.sub(r"\s+", " ", re.sub(r"\d+", "#", line))[:400]
            count = seen.get(key, 1)
            out.append(f"  [x{count}] {line}" if count > 1 else f"  {line}")
        if len(order) > _MAX_ERROR_LINES:
            out.append(f"  ... {len(order) - _MAX_ERROR_LINES} more distinct lines omitted")
    if traces:
        out.append(f"-- exception traces from {source} --")
        for trace in traces:
            out.append(trace)
            out.append("")
    if not out:
        out.append(f"-- {source}: no ERROR/WARN lines found in "
                   f"{stats['lines']} lines --")
    return "\n".join(out), stats


# --------------------------------------------------------------------------
# Thread dumps
# --------------------------------------------------------------------------

_TSTATE_RE = re.compile(r"java\.lang\.Thread\.State:\s+(\w+)")
_TNAME_RE = re.compile(r'^"([^"]+)"')
_DEADLOCK_RE = re.compile(r"Found (?:one Java-level )?deadlock", re.IGNORECASE)


def analyze_thread_dump(text: str, source: str) -> str:
    """Summarize a jstack dump: state histogram, deadlocks, and where the
    blocked threads are actually stuck."""
    states: dict[str, int] = {}
    # Stack frame -> how many threads are sitting on it, for BLOCKED/WAITING only.
    hotspots: dict[str, int] = {}
    total = 0

    current_name = ""
    current_state = ""
    for line in text.splitlines():
        name = _TNAME_RE.match(line)
        if name:
            current_name = name.group(1)
            current_state = ""
            total += 1
            continue
        st = _TSTATE_RE.search(line)
        if st:
            current_state = st.group(1)
            states[current_state] = states.get(current_state, 0) + 1
            continue
        if current_state in ("BLOCKED", "WAITING", "TIMED_WAITING") and \
                line.strip().startswith("at "):
            frame = line.strip()
            if frame not in hotspots:
                hotspots[frame] = 0
            hotspots[frame] += 1
            current_state = ""      # only the topmost frame counts

    out = [f"-- thread dump {source}: {total} threads --"]
    if states:
        histogram = ", ".join(f"{k}={v}" for k, v in
                              sorted(states.items(), key=lambda kv: -kv[1]))
        out.append(f"  states: {histogram}")
    deadlocks = len(_DEADLOCK_RE.findall(text))
    if deadlocks:
        out.append(f"  *** {deadlocks} DEADLOCK section(s) reported by the JVM ***")
        idx = text.lower().find("found")
        out.append("  " + text[idx:idx + 1200].replace("\n", "\n  "))
    top = sorted(hotspots.items(), key=lambda kv: -kv[1])[:8]
    if top:
        out.append("  where blocked/waiting threads are parked:")
        for frame, count in top:
            out.append(f"    [{count} threads] {frame}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Bundle loading
# --------------------------------------------------------------------------

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".svn"}


def load_bundle(directory: str | Path) -> BundleResult:
    """Read a case directory and synthesize a single case description."""
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Not a case directory: {root}")

    result = BundleResult()
    sections: list[str] = []
    scenario = ""

    # `.gitkeep` and friends only exist to keep an empty input/ subdirectory in
    # git. Ingesting them produces a zero-line "log" whose empty error summary
    # then wins the error_signature, so drop dot-files and zero-byte files.
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and not any(part in _SKIP_DIRS for part in p.parts)
        and not p.name.startswith(".")
        and p.stat().st_size > 0
    )
    if not files:
        raise ValueError(
            f"Case directory has no readable files: {root}\n"
            f"At minimum it needs a case.txt describing the scenario.")

    redaction_hits = 0
    log_blocks: list[str] = []
    dump_blocks: list[str] = []

    for path in files:
        rel = path.relative_to(root).as_posix()
        size = path.stat().st_size
        kind = _classify(path, root)

        # The scenario file is the spine of the case.
        if kind in ("config", "serverlog", "other") and \
                path.name.lower() in ("case.txt", "case.md", "description.txt",
                                      "customer-case.txt", "scenario.txt"):
            scenario, hits = redact(_read_text(path))
            redaction_hits += hits
            result.manifest.append(f"{rel:<44} scenario ({_human_size(size)})")
            continue

        if kind == "heapdump":
            # Never read: binary, and routinely larger than RAM. Record it so the
            # gap analysis can say what evidence exists and how to open it.
            result.manifest.append(f"{rel:<44} heap dump ({_human_size(size)}) -- NOT parsed")
            dump_blocks.append(
                f"-- heap dump present: {rel} ({_human_size(size)}) --\n"
                f"  Binary; this agent does not parse it. Open with Eclipse MAT or\n"
                f"  `jhat`/`jcmd GC.heap_info`, then paste the dominator tree or the\n"
                f"  leak suspect report into case.txt as text."
            )
            continue

        if kind == "archive":
            result.manifest.append(f"{rel:<44} archive ({_human_size(size)}) -- not extracted")
            result.warnings.append(
                f"{rel} is an archive; extract it into the case directory so its "
                f"contents can be read.")
            continue

        if _is_binary(path):
            result.manifest.append(f"{rel:<44} binary ({_human_size(size)}) -- skipped")
            continue

        raw = _read_text(path)
        raw, hits = redact(raw)
        redaction_hits += hits

        if not result.detected_product:
            product, version = _detect_product(raw)
            if product:
                result.detected_product, result.detected_version = product, version

        if kind == "config":
            if len(raw) > _MAX_CONFIG_CHARS:
                result.warnings.append(
                    f"{rel} is {_human_size(size)}; only the first "
                    f"{_MAX_CONFIG_CHARS} characters were kept.")
                raw = raw[:_MAX_CONFIG_CHARS]
            result.config_files[path.name] = raw
            result.manifest.append(f"{rel:<44} config ({_human_size(size)})")
        elif kind == "threaddump":
            dump_blocks.append(analyze_thread_dump(raw, rel))
            result.manifest.append(f"{rel:<44} thread dump ({_human_size(size)})")
        elif kind == "gclog":
            tail = "\n".join(raw.splitlines()[-40:])
            log_blocks.append(f"-- last 40 lines of GC log {rel} --\n{tail}")
            result.manifest.append(f"{rel:<44} GC log ({_human_size(size)})")
        elif kind == "accesslog":
            lines = raw.splitlines()
            codes: dict[str, int] = {}
            for line in lines:
                m = re.search(r'"\s+(\d{3})\s', line) or re.search(r"\s(\d{3})\s+\d+\s*$", line)
                if m:
                    codes[m.group(1)] = codes.get(m.group(1), 0) + 1
            summary = ", ".join(f"{k}={v}" for k, v in sorted(codes.items()))
            log_blocks.append(
                f"-- access log {rel}: {len(lines)} requests, status codes: "
                f"{summary or 'none parsed'} --\n"
                + "\n".join(lines[-15:]))
            result.manifest.append(f"{rel:<44} access log ({_human_size(size)})")
        else:  # serverlog / other text
            distilled, stats = distill_log(raw, rel)
            log_blocks.append(distilled)
            result.manifest.append(
                f"{rel:<44} log ({_human_size(size)}, {stats['lines']} lines, "
                f"{stats['errors']} error / {stats['warnings']} warn)")

    # ---- assemble ----------------------------------------------------------
    if scenario:
        sections.append(scenario)
    else:
        result.warnings.append(
            "No case.txt found. The scenario description is what tells the agent "
            "what to reproduce; logs alone cannot supply it.")

    # A version read out of the customer's own boot banner beats a typed one.
    if result.detected_product and "product:" not in scenario.lower():
        sections.append(
            f"Product: {result.detected_product}\n"
            f"Version: {result.detected_version}")
    elif result.detected_version:
        # Both are present: if they disagree, say so now. Chasing a bug on the
        # version someone typed while the logs came from another one is a
        # guaranteed wasted day.
        stated = re.search(r"^\s*version\s*[:=]\s*(\S+)", scenario,
                           re.IGNORECASE | re.MULTILINE)
        if stated and not (stated.group(1).startswith(result.detected_version)
                           or result.detected_version.startswith(stated.group(1))):
            result.warnings.append(
                f"case.txt says version {stated.group(1)} but the log banner says "
                f"{result.detected_product} {result.detected_version}. The typed "
                f"value is being used -- correct case.txt if the logs are right.")

    if result.config_files:
        sections.append(
            "Configuration files supplied by the customer:\n  "
            + "\n  ".join(sorted(result.config_files)))
    if log_blocks:
        sections.append("Error Messages:\n" + "\n\n".join(log_blocks))
    if dump_blocks:
        sections.append("Thread and heap dump analysis:\n" + "\n\n".join(dump_blocks))

    result.text = "\n\n".join(sections)
    if redaction_hits:
        result.warnings.append(
            f"Redacted {redaction_hits} credential-looking value(s) before analysis.")
    return result
