# JVM Issue Reproducer (EAP / Data Grid)

A self-contained reproducer for **JVM-class** issues: heap `OutOfMemoryError`,
memory leaks, GC pressure, high CPU, and thread exhaustion. It deploys a stress
servlet to EAP, runs the JVM with full diagnostics enabled, and captures the exact
artifacts Red Hat support needs (heap dumps, thread dumps, GC logs).

## Reproduction Requirements & Gap Analysis

Confidence: **partial** — the mechanism reproduces locally, but matching a *specific*
customer JVM incident needs artifacts only the customer's environment provides.

This project already provides:
- ✅ A workload that triggers each JVM failure mode on demand
- ✅ JVM flags for automatic heap dump on OOM, GC logging, capped metaspace
- ✅ A capture script producing 3 thread dumps + a live heap dump

You must still supply, to match a real case:
- ❗ **Exact JDK vendor + version** (e.g. `OpenJDK 17.0.9 Temurin`) — GC/leak behavior is version-specific
- ❗ The customer's **`-Xmx` / JVM flags** (set `HEAP=` and `JAVA_OPTS=` when running)
- ❗ For a leak: the customer's **heap dump** (`.hprof`) to identify the real leaking type
- ❗ The customer's **workload profile** (request mix / rate)

## Run

```bash
export EAP_HOME=/path/to/jboss-eap
export HEAP=256m          # optional; small = OOM triggers fast
./build.sh
./run.sh
```

## Reproduce a mode

```bash
./test.sh heap-oom   # heap OutOfMemoryError -> auto .hprof in dumps/
./test.sh leak       # slow leak; watch heapPctUsed climb
./test.sh cpu        # high CPU; captures thread dumps of the busy threads
./test.sh threads    # thread exhaustion
./test.sh gc         # GC pressure; see gc.log pauses
```

Watch live: `http://localhost:8080/jvm-reproducer/jvm?mode=status`

## Capture diagnostics (the case artifacts)

```bash
./capture.sh     # -> diagnostics/<timestamp>/ : threaddump-{1,2,3}.txt, heap-live.hprof, heap-info.txt, gc.log
```

- Heap dump `.hprof` → open in **Eclipse MAT**, look at the dominator tree.
- Thread dumps → `grep -A20 RUNNABLE threaddump-*.txt` to find busy threads (high CPU).
- GC log → `grep Pause gc.log`, or load into GCViewer.

## Using this for Red Hat Data Grid (server) JVM issues

The stress servlet is EAP-specific, but the **diagnostics approach is identical**:
put the same `JAVA_OPTS` (from `run.sh`) into `DATAGRID_HOME/bin/server.conf`, drive
load via Hot Rod / REST, and run `./capture.sh` against the Data Grid JVM. Common
Data Grid JVM triggers: unbounded cache with no eviction (`max-size`/`max-count`),
large `OFF_HEAP` sizing, and GC pauses longer than JGroups `FD_ALL` timeouts (which
eject nodes and can look like a cluster split).
