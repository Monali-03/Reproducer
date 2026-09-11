# Reproducer: java.lang.IllegalStateException: Session already invalidated Issue

## Issue Summary

- **Product:** JBoss EAP 7.4.14
- **Subsystem:** infinispan
- **Categories:** clustering, session, cache, web
- **Server Config:** standalone-ha.xml
- **Nodes:** 3
- **Error:** `java.lang.IllegalStateException: Session already invalidated`
- **Root Cause:** Session replication or failover issue in clustered environment

## Reproduction Requirements & Gap Analysis

**Reproduction confidence:** PARTIAL -- reproducer sets up the conditions, but the bug is timing/version sensitive and may not fire every run.

To reproduce this issue reliably, the following are required:

- [ ] mod_cluster / Apache load balancer with sticky sessions (bundled by this reproducer)
- [ ] >=3 nodes when the web cache uses owners=2 so sessions are not on every node (bundled by this reproducer)
- [ ] Concurrent load applied during the failover window (bundled by this reproducer's test)

**Provided by this reproducer (already satisfied):**

- [x] 3-node HA cluster (start-cluster.sh)
- [x] Apache sticky-session load balancer (lb/start-lb.sh)
- [x] Undertow instance-id / JSESSIONID routing (instance-id.cli)
- [x] Concurrent in-flight load + abrupt node kill (test.sh)

> Any item left unchecked above must be supplied from the customer environment (exact build, JVM flags, heap/thread dumps, or workload) for a faithful reproduction.

## Environment Requirements

- JBoss EAP 7.4.14
- JDK: OpenJDK 11.0.21
- OS: RHEL 8.9
- Maven 3.8+

## Topology

Customer: 2-node Standalone HA (2 nodes behind Apache mod_cluster) environment. Reproducer: 2-node cluster.

```
Client
  |
  v
EAP Node 1 ---- EAP Node 2 ---- EAP Node 3
  node1: HTTP=8080, mgmt=9990
  node2: HTTP=8180, mgmt=10090
  node3: HTTP=8280, mgmt=10190
```

## Quick Start (one command)

```bash
./run-reproducer.sh --repeat 5
```

You do not set `EAP_HOME`. This package was generated from a case that
says **JBoss EAP 7.4.14**, and `ensure-eap-home.sh` finds an
installation of that major version on this machine and uses it. If
`EAP_HOME` is already set to a different major version the run **aborts**
rather than producing a meaningless result -- a javax application
on the other major version deploys "OK" and then 404s on every request.
Point it at a non-standard location with
`EAP_SEARCH_PATHS=/where/i/keep/eap`.

That provisions a compatible JDK (downloading one if this machine has
none), builds the app, brings up all 3 nodes **each in its own
terminal window**, starts the load balancer, runs the test and prints a
verdict. `--repeat` re-runs the whole cycle, which matters because the
failure is intermittent. Add `--no-terminals` over SSH or in CI.

You do not need to set `JAVA_HOME`. `ensure-jdk.sh` reads
`$EAP_HOME/version.txt` and picks a JDK the server can actually boot:
JDK 8-11 for EAP 7.x (the legacy `security` subsystem and
`<security-realms>` in the stock configs are rejected on JDK 14+, so the
server dies at boot with WFLYSRV0056), JDK 17-21 for EAP 8.x. If nothing
suitable is installed it fetches a Temurin build into `~/jdks`.

## Steps to Reproduce (manual)

1. Check which installation will be used:
   ```bash
   ./ensure-eap-home.sh          # prints the version it picked, or why it refused
   ```

2. Build the application:
   ```bash
   chmod +x *.sh
   ./build.sh
   ```

3. Start the 3-node cluster (one terminal window per node):
   ```bash
   ./start-cluster.sh
   # or add a single node by hand:
   ./run-node.sh node2 100 standalone-node2
   ```

4. Start the sticky load balancer (bundled):
   ```bash
   ./lb/start-lb.sh   # Apache mod_proxy_balancer on :8000
   ```

5. Trigger the issue (drives load through the LB, kills a node):
   ```bash
   ./test.sh
   ```

6. Tear down:
   ```bash
   ./lb/stop-lb.sh
   ./stop-cluster.sh
   ```

## Expected vs Actual Behavior

**Expected:** Session should fail over to the surviving node seamlessly. The user should continue
their shopping session without being redirected to the login page.
**Actual:** Session is lost and user must re-login. Shopping cart data is lost. The issue occurs
approximately 70% of the time during restarts. When it does not occur, the failover
works correctly.

## Expected Error

```
java.lang.IllegalStateException: Session already invalidated
```

## Log Locations

- node1: `./node1.log` and `$EAP_HOME/standalone/log/server.log`
- node2: `./node2.log` and `$EAP_HOME/standalone/log/server.log`
- node3: `./node3.log` and `$EAP_HOME/standalone/log/server.log`

## Validation Status

NOT EXECUTED -- generated based on analysis
