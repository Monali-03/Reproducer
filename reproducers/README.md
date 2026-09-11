# Reproducer workspaces

Four standing folders, one per product. Drop a case into the matching folder's
`input/` and run its `run.sh`. Nothing else to set up.

| Folder | Product | Server it runs on |
| --- | --- | --- |
| `eap7/` | JBoss EAP 7.x | a 7.x install found on this machine |
| `eap8/` | JBoss EAP 8.x | an 8.x install found on this machine |
| `datagrid/` | Red Hat Data Grid 8.x | a Data Grid server install |
| `jvm/` | heap / GC / metaspace / thread issues | no server at all — one JVM |

---

## Where the attachments go

```
reproducers/<workspace>/input/
├── case.txt          the scenario                       REQUIRED
├── configs/          standalone-ha.xml, infinispan.xml, httpd.conf,
│                     jboss-web.xml, persistence.xml, *.cli, *.properties
├── logs/             server.log, boot.log, gc.log, error_log, access_log,
│                     catalina.out
├── dumps/            threaddump-*.txt / jstack output, *.hprof heap dumps
└── attachments/      anything else worth keeping
```

`run.sh` prints a count per subdirectory before it generates, so you can see at
a glance that it picked your files up:

```
  input     : /home/mopatil/reproducer-agent/reproducers/eap7/input
    configs/     2 file(s)
    logs/        3 file(s)
    dumps/       1 file(s)
    attachments/ 0 file(s)
```

Everything except `case.txt` is optional. `case.txt` is the one thing logs
cannot supply — it is the claim being tested.

Each workspace ships with a **sample** `input/case.txt` so you can run it
immediately and see what a working run looks like. Overwrite it with the real
case; there is a blank template at `reproducers/_TEMPLATE-case.txt`:

```bash
cp reproducers/_TEMPLATE-case.txt reproducers/eap7/input/case.txt
```

What happens to each kind of artifact — distillation, thread-dump analysis,
redaction, what is deliberately *not* parsed — is documented in
[../cases/README.md](../cases/README.md). The rules are identical here; the
workspaces just save you creating a case directory each time.

---

## Running

```bash
cd reproducers/eap7
./run.sh                 # generate from input/ and run once
./run.sh --repeat 5      # run five times and report a hit rate
./run.sh --no-terminals  # keep every node in this terminal (use over SSH)
./run.sh --generate-only # build the package, do not run it
./run.sh --regenerate    # rebuild generated/ from scratch
```

The generated package lands in `<workspace>/generated/` and is reused until
`input/case.txt` changes. To drive it directly:
`generated/run-reproducer.sh --repeat 5`.

---

## The version comes from the case file, not from your shell

Set `EAP_HOME` and `JAVA_HOME` once in `~/.bashrc` and forget about them. Each
workspace pins its own product and major version, and resolves an installation
in this order:

1. **A product-specific variable** — `EAP7_HOME`, `EAP8_HOME`, `DATAGRID_HOME`,
   `RHDG_HOME`, `INFINISPAN_HOME`. If you set one of these and it points at the
   wrong version, the run **stops**. You said exactly which install to use, so
   using a different one silently would produce a meaningless result.
2. **An ambient variable** — `EAP_HOME`, `JBOSS_HOME`, `SERVER_HOME`. These come
   from your shell profile and are not a statement about this case. If one
   points at the wrong version it is reported and **skipped**, not treated as an
   error:

   ```
   [server-home] $EAP_HOME points at 8.1, but this reproducer targets JBoss EAP 7.4.14.
   [server-home]   Ignoring it and looking for a 7.x install instead.
   ```

   That is what lets one `~/.bashrc` serve all four workspaces.
3. **Autodiscovery** — `$HOME/Documents/EAP_lab`, `$HOME/Documents/Datagrid`,
   `$HOME/EAP`, `$HOME/eap`, `$HOME/jboss`, `$HOME/Downloads`, `/opt/jboss`,
   `/opt/eap`, `/opt/datagrid`, `/opt`. Point `EAP_SEARCH_PATHS` somewhere else
   if your installs live outside those.

A micro-version mismatch (case says 7.4.14, install is 7.4.23) warns but
continues — with the reminder that a CP-specific regression may not show up.

The JDK is resolved the same way and **downloaded** into `~/jdks` if the machine
has none that can boot the target: EAP 7.4 needs 8–11 (its legacy `security`
subsystem is rejected on JDK 14+), EAP 8.x and Data Grid need 17–21.

`run.sh` also refuses to start if `input/case.txt` disagrees with the workspace:

```
ERROR: input/case.txt says version 8.0.1, but this is the
       JBoss EAP 7.x workspace.
```

---

## What each workspace actually does

**`eap7/`, `eap8/`** — builds a minimal war (javax for 7, jakarta for 8), seeds
one `jboss.server.base.dir` per node from the stock `standalone/`, starts each
node in its own terminal window, and for session/clustering cases puts a real
Apache sticky-session load balancer on `:8000` in front. `test.sh` drives
traffic through the LB, kills the node holding the session and checks whether
the session survived.

**`datagrid/`** — starts a real N-node Data Grid cluster, each with its own
server root and endpoint user. Discovery is TCPPING with the peers listed
explicitly rather than the stock multicast MPING, which finds nothing on a
laptop and leaves every node in a cluster of one. `test.sh` creates a
distributed cache with `owners=2`, writes N entries, `kill -9`s an owner, waits
for the new view and reads every key back.

**`jvm/`** — compiles one `Workload.java` and runs it under the flags in
`jvm.env` with `-XX:+HeapDumpOnOutOfMemoryError`, a GC log and periodic thread
dumps. Three modes: `heap` (OOM: Java heap space), `metaspace` (a classloader
leak → OOM: Metaspace) and `threads` (a real deadlock, reported by
`ThreadMXBean.findDeadlockedThreads`). Edit `jvm.env` first — `-Xmx` and the
collector usually decide whether the failure happens at all.

---

## Reading the verdict

Every workspace ends with one of three outcomes, and they mean different things:

- **`ISSUE REPRODUCED`** — the failure happened here.
- **`Issue NOT reproduced`** — it did not, and that is a real result: on a
  version-matched install it is evidence the bug is not present in that build.
  Check the micro version before concluding.
- **`INCONCLUSIVE`** — the reproducer could not run far enough to tell. A failed
  cache creation, a node that would not die, reads coming back 403 instead of
  404. These are *not* counted as either outcome, because a broken setup that
  looks like a failure is worse than no answer.

---

## Running two workspaces at once

Don't, yet. `eap7/` and `eap8/` both use node names `node1..n`, LB port 8000 and
the same JGroups multicast group, so they will find each other and fight over
ports. Finish one, then run the other. `datagrid/` and `jvm/` do not collide
with the EAP workspaces.
