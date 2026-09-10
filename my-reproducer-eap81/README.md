# Reproducer: session lost on failover — JBoss EAP 8.1

Port of `../my-reproducer/` (EAP 7.4) to **EAP 8.1 / Jakarta EE 10**, to check
whether the customer's session-loss-on-failover behaviour still exists there.

## Issue Summary

- **Product:** JBoss EAP 8.1
- **Subsystem:** infinispan / distributable web sessions
- **Server Config:** `standalone-ha.xml`, 3 nodes, `owners=2`
- **Symptom:** after a sticky node dies mid-request the user lands on a survivor
  with no session — `isNew=true`, cart empty, redirect to login.

## Quick Start (one command)

```bash
export EAP_HOME=~/Documents/EAP_lab/jboss-eap-8.1.0/jboss-eap-8.1
./run-reproducer.sh --repeat 5
```

Provisions a JDK, builds the app, brings up all 3 nodes **each in its own
terminal window**, starts the sticky load balancer, runs the failover test and
prints how many attempts reproduced the issue. Add `--no-terminals` over SSH.

You do not need to set `JAVA_HOME`. `ensure-jdk.sh` reads `$EAP_HOME/version.txt`
and resolves a JDK the server can actually boot — **17–21 for EAP 8.x**, 8–11 for
EAP 7.x — downloading a Temurin build into `~/jdks` if the machine has none.

## Adding a node by hand

Each node needs its **own** `jboss.server.base.dir`. A port offset alone is not
enough: instances sharing `$EAP_HOME/standalone` fight over `data/`, `tmp/`,
`log/` and the deployment marker files.

```bash
./run-node.sh node2 100 standalone-node2   # seeds the base dir, deploys, runs
```

`nodes.env` is the single source of truth for the topology (name, offset, HTTP
port, management port, base dir) and is sourced by every script here.

## Topology

```
Client --> Apache mod_proxy_balancer :8000 (sticky JSESSIONID)
              |            |            |
           node1        node2        node3
        HTTP  8080       8180         8280
        mgmt  9990      10090        10190
        base  standalone-node1/-node2/-node3
```

Three nodes, not two: the web cache uses `owners=2`, so with only two nodes every
node holds every session and failover can never miss. The third node makes it
possible for a failover to land somewhere that must still fetch state.

## Difference from the EAP 7.4 reproducer

EAP 8.1 is Jakarta EE 10. The 7.4 application (`javax.servlet`,
`javaee-api 8.0.1`, `web-app 4.0`) **deploys without error on 8.1 but every
servlet returns 404** — the `@WebServlet` annotations are in the wrong namespace
and are never scanned, so the war becomes an empty context that still looks
healthy in the management console. This copy uses `jakarta.servlet`,
`jakarta.jakartaee-web-api 10.0.0`, `web-app 6.0` and compiler release 17.

Check the endpoint, not the deployment state:

```bash
curl -s http://localhost:8080/reproducer/session   # must print node=/sessionId=/counter=
```

## Reproduction Requirements & Gap Analysis

| Requirement | Status |
| --- | --- |
| 3-node HA cluster | bundled (`start-cluster.sh`) |
| Sticky load balancer | bundled (`lb/`, Apache mod_proxy_balancer on :8000) |
| Session workload + mid-flight kill | bundled (`test.sh`) |
| Compatible JDK | provisioned automatically (`ensure-jdk.sh`) |
| Customer's exact EAP micro-version | **not matched** — this is stock 8.1 |
| Customer heap/thread dumps, real workload | **not available** |

## Result on this build

Not reproduced. Across the attempts run here the session survived every
failover (`isNew=false`, cart intact) — replication and sticky failover behave
correctly on EAP 8.1. Treat that as a finding, not a failure of the reproducer:
it indicates the regression is not present in 8.1. Confirm against the
customer's exact micro-version before advising an upgrade.
