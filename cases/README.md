# Where to put customer artifacts

One directory per case. Copy the template and fill it in:

```bash
cp -r cases/_TEMPLATE cases/04123456        # use the real case number
```

Then point the agent at the **directory**, not a file:

```bash
python reproduce.py generate -i cases/04123456 -o my-reproducer-04123456
```

## Layout

```
cases/04123456/
├── case.txt          the scenario  (REQUIRED -- nothing else replaces it)
├── configs/          standalone-ha.xml, domain.xml, httpd.conf, infinispan.xml,
│                     jboss-web.xml, web.xml, persistence.xml, *.cli, *.properties
├── logs/             server.log, boot.log, gc.log, error_log, access_log,
│                     Data Grid server.log, Tomcat catalina.out
├── dumps/            threaddump-*.txt / jstack output, *.hprof heap dumps
└── attachments/      anything else worth keeping
```

A flat directory works too — files are classified by name and extension, so
`server.log` and `standalone-ha.xml` dropped loose are still recognized. The
subdirectories just make a large case easier to read.

Everything except `case.txt` is optional. Give what you have.

## What happens to each artifact

| You drop | The agent does |
| --- | --- |
| `case.txt` | Parses product, version, JDK, OS, node count, topology, expected vs actual |
| `configs/*.xml`, `*.conf`, `*.properties` | Read verbatim and copied into the generated package as `customer-configs/`, with a `diff-customer-config.sh` that diffs each one against the stock file the reproducer runs on. They are **not** applied automatically — their config carries their datasources, realms and bind addresses and usually will not boot elsewhere. The value is the diff: if the reproducer passes on stock and fails on theirs, the difference is the bug |
| `logs/server.log` | Distilled: every distinct ERROR/WARN and product code (WFLY/ISPN/JGRP/JBAS/UT/AH), repeats collapsed to `[xN]`, plus up to 6 exception traces. Boot milestones (WFLYSRV0025/0049/0056, ISPN000094 view changes) are kept even at INFO |
| `logs/access_log` | Status-code histogram plus the last 15 requests — this is what shows the 502-then-302 signature of a failover that dropped the session |
| `logs/gc.log` | Last 40 lines |
| `dumps/threaddump-*.txt` | Thread-state histogram, **JVM-reported deadlocks quoted in full**, and the stack frames where blocked threads are actually parked, ranked by thread count |
| `dumps/*.hprof` | **Not parsed** — binary, routinely bigger than RAM. Recorded in the manifest so the gap analysis knows it exists. Open it in Eclipse MAT and paste the leak-suspect report into `case.txt` as text |
| `*.zip`, `*.tar.gz` | **Not extracted.** Unpack into the case directory yourself |
| Anything binary | Skipped |

## Two things it does automatically

**Credentials are redacted before anything is read or sent anywhere.**
`password=`, `secret=`, `token=`, `Authorization:`, PEM private keys and email
addresses are replaced with `***REDACTED***`. Hostnames and IPs are deliberately
kept — they are needed to work out the topology and they are not secrets. The
run reports how many values it redacted. This is best-effort pattern matching,
not a guarantee: skim a `sosreport` yourself before dropping it in.

**The product and version are read from the customer's own boot banner.**
`WFLYSRV0049: JBoss EAP 7.4.14.GA ...` in `server.log` is more trustworthy than
what anyone typed. If the banner and `case.txt` disagree, the run warns you and
uses the typed value:

```
! case.txt says version 7.4.14 but the log banner says JBoss EAP 8.1.
  The typed value is being used -- correct case.txt if the logs are right.
```

## What you get back

Before analysis, a manifest of exactly what was read — so nothing is silently
ignored:

```
  case.txt                        scenario (194B)
  configs/standalone-ha.xml       config (423B)
  dumps/heapdump.hprof            heap dump (2.9MB) -- NOT parsed
  dumps/threaddump-1.txt          thread dump (868B)
  logs/access_log                 access log (211B)
  logs/server.log                 log (84.7KB, 485 lines, 1 error / 17 warn)
```

## Products other than EAP

The same layout applies to Data Grid, JWS/Tomcat, Apache httpd and OpenShift
cases — only the filenames change. Useful additions per product:

- **Data Grid:** `configs/infinispan.xml`, `logs/server.log`, and the output of
  `bin/cli.sh --connect ... 'describe caches/<name>'`
- **JWS / Tomcat:** `configs/server.xml`, `configs/context.xml`,
  `logs/catalina.out`, `logs/localhost_access_log*.txt`
- **Apache httpd / mod_cluster:** `configs/httpd.conf`, `configs/*.conf` from
  `conf.d/`, `logs/error_log`, `logs/access_log`, and the balancer-manager page
  saved as text
- **OpenShift:** `oc get pods -o yaml`, `oc logs <pod>` into `logs/`, and the
  deployment/statefulset YAML into `configs/`

## Still needed from a human

The bundle cannot supply these, so put them in `case.txt` if you know them:

- Which **exact micro version / cumulative patch** the customer runs, and which
  version it last worked on. A "7.4.10 → 7.4.14 regression" is a completely
  different investigation from "broken since install".
- **Load and concurrency** — 5 users or 5,000; session size; how many objects.
- **Whether the customer can reproduce on demand**, or whether it happens once
  a week under production traffic. That decides whether a reproducer is even
  the right tool.
