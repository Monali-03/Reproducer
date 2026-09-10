# Reproducer: java.lang.IllegalStateException: Session already invalidated Issue

## Issue Summary

- **Product:** JBoss EAP 7.4.14
- **Subsystem:** infinispan
- **Categories:** clustering, session, cache, web
- **Server Config:** standalone-ha.xml
- **Nodes:** 2
- **Error:** `java.lang.IllegalStateException: Session already invalidated`
- **Root Cause:** Session replication or failover issue in clustered environment

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
EAP Node 1 ---- EAP Node 2
  node1: HTTP=8080, mgmt=9990
  node2: HTTP=8180, mgmt=10090
```

## Steps to Reproduce

1. Set environment:
   ```bash
   export EAP_HOME=/path/to/jboss-eap
   export JAVA_HOME=/path/to/jdk
   ```

2. Build the application:
   ```bash
   chmod +x *.sh
   ./build.sh
   ```

3. Start the 2-node cluster:
   ```bash
   ./start-cluster.sh
   ```

4. Trigger the issue:
   ```bash
   ./test.sh
   ```

5. Stop the cluster:
   ```bash
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

## Validation Status

NOT EXECUTED -- generated based on analysis
