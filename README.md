# Red Hat Support Reproducer Agent

AI-powered tool that analyzes customer support cases and generates minimal, runnable reproducer environments for Red Hat middleware products. The agent reads a customer case description, identifies the product, version, and symptoms, then produces a self-contained project that reproduces the reported issue in a local or OpenShift environment.

---

## Supported Products

| Product | Versions | Reproducer Types |
|---------|----------|-----------------|
| JBoss EAP | 7.x, 8.x | Local standalone, standalone-ha, domain, OpenShift |
| Red Hat Data Grid / Infinispan | 8.x | Local cluster, OpenShift StatefulSet, cross-site |
| Red Hat JBoss Web Server / Apache HTTP Server | 5.x, 6.x | Local httpd, mod_cluster, mod_jk |
| OpenShift application/runtime issues | 4.x | Deployment, Route, Service, BuildConfig |
| JGroups | 5.x | TCP/UDP stacks, RELAY2, custom protocols |
| Elytron / Security | EAP 7.x, 8.x | Security domains, SASL, credential stores |
| TLS/SSL | All | Keystore generation, mutual TLS, certificate chains |

**Common issue categories:** deployment failures, classloading conflicts, session replication, clustering, load balancing, cross-site replication, security configuration, performance, thread pools, datasource configuration, messaging (ActiveMQ Artemis), and transaction recovery.

---

## Prerequisites

- **Python 3.9+**
- **Anthropic API key** -- set as the `ANTHROPIC_API_KEY` environment variable
- **For building reproducers:**
  - JDK 11 or 17 (JDK 21 for EAP 8.x reproducers)
  - Maven 3.8+
  - Docker or Podman (for containerized reproducers)
- **For OpenShift reproducers:**
  - `oc` CLI authenticated to a cluster
  - A project/namespace with appropriate permissions

---

## Installation

```bash
cd reproducer-agent
pip install -r requirements.txt
export ANTHROPIC_API_KEY="your-key-here"
```

Verify the installation:

```bash
python reproduce.py --version
```

---

## Quick Start

### 1. Generate a reproducer from a case file

```bash
python reproduce.py generate -i customer-case.txt -o ./my-reproducer
```

This reads the case description, analyzes the issue, and writes a complete reproducer project to `./my-reproducer/`.

### 2. Analyze only (no file generation)

```bash
python reproduce.py analyze -i customer-case.txt
```

Prints a structured analysis of the case -- identified product, version, root cause hypothesis, and recommended reproducer strategy -- without generating any files.

### 3. Validate a generated reproducer

```bash
python reproduce.py validate ./my-reproducer
```

Checks that the reproducer builds, required configuration files are present, and (if Docker/Podman is available) the containers start without errors.

### 4. Pipe from stdin

```bash
cat case.txt | python reproduce.py generate -o ./output
```

Useful for integrating with case management tools or scripts that export case text.

### 5. Override product and version

```bash
python reproduce.py generate -i case.txt -p "EAP" -v "8.0" -o ./output
```

When the case text is ambiguous or you want to force a specific product version, use `-p` and `-v` to override detection.

---

## Input Format

The agent accepts customer case descriptions in several formats. The more detail provided, the better the reproducer.

### Case bundles (raw customer artifacts)

`-i` accepts a **directory** as well as a file. Drop the whole pile from the
case — server logs, config XMLs, thread dumps, heap dumps, httpd logs — into
one directory and point at it:

```bash
cp -r cases/_TEMPLATE cases/04123456
# ... fill in case.txt, drop artifacts into configs/ logs/ dumps/ ...
python reproduce.py generate -i cases/04123456 -o my-reproducer-04123456
```

Logs are distilled rather than pasted (distinct ERROR/WARN and WFLY/ISPN/JGRP
codes with repeat counts, plus exception traces), thread dumps are summarized
with their deadlocks quoted, heap dumps are recorded but not parsed, and
credentials are redacted before anything is read. The product and version are
cross-checked against the boot banner in the customer's own log.

See [cases/README.md](cases/README.md) for the layout and what happens to each
kind of artifact.

### Structured format (labeled fields)

```
Product: JBoss EAP
Version: 7.4.14
JDK: OpenJDK 11.0.21
OS: RHEL 8.9

Issue Description:
Application fails to start after upgrading from EAP 7.3 to 7.4.

Error Messages:
ERROR [org.jboss.as.server] WFLYSRV0coords: Deploy failed

Stack Trace:
java.lang.NoClassDefFoundError: com/example/MyClass
    at com.example.Main.init(Main.java:42)
```

### Free-form format

```
Customer is running EAP 7.4.14 on RHEL 8.9 with OpenJDK 11. They upgraded from
7.3 and now the application won't start. The server log shows a
NoClassDefFoundError for com.example.MyClass during deployment. They have a
custom module that depends on a removed subsystem.
```

### Mixed format

```
Product: JBoss EAP 7.4.14
Environment: RHEL 8.9, OpenJDK 11

The customer's application deploys fine on node1 but fails on node2 in a
standalone-ha configuration. They see ClassNotFoundException intermittently.
Attaching the server.log from node2.

Key error from log:
  WFLYCTL0coords: coords coords coords
```

All formats are supported. The agent uses AI to extract product, version, symptoms, error messages, and configuration details regardless of structure.

---

## Output Structure

### Local VM reproducer

```
my-reproducer/
  README.md                      # How to run this specific reproducer
  pom.xml                        # Maven project (for Java-based reproducers)
  src/
    main/
      java/
        com/reproducer/...       # Minimal application code
      webapp/
        WEB-INF/
          web.xml
          jboss-web.xml
  config/
    standalone-ha.xml            # Server configuration (modified from default)
    jgroups-tcp.xml              # JGroups stack (if clustering)
  scripts/
    setup.sh                     # Environment setup (download server, etc.)
    run.sh                       # Start the reproducer
    test.sh                      # Validate the issue is reproduced
  docker-compose.yml             # Optional: containerized multi-node setup
```

### OpenShift reproducer

```
my-reproducer/
  README.md
  pom.xml
  src/
    main/...
  openshift/
    namespace.yaml
    deployment.yaml
    service.yaml
    route.yaml
    configmap.yaml               # Server configuration as ConfigMap
    buildconfig.yaml             # S2I or Dockerfile build
  scripts/
    deploy.sh                    # oc apply -f openshift/
    test.sh                      # Curl-based validation
    cleanup.sh                   # oc delete project ...
```

### Combined (local + OpenShift)

When the issue could manifest in both environments, both directories are produced:

```
my-reproducer/
  README.md
  pom.xml
  src/...
  config/...
  scripts/...
  docker-compose.yml
  openshift/...
```

---

## Commands Reference

| Command | Description |
|---------|-------------|
| `generate` | Analyze a case and generate a complete reproducer project |
| `analyze` | Analyze a case and print findings without generating files |
| `validate` | Check that a generated reproducer builds and runs |
| `list-templates` | List all available reproducer templates |
| `show-template` | Display the contents of a specific template |

### Global options

| Option | Short | Description |
|--------|-------|-------------|
| `--input` | `-i` | Path to customer case file (or `-` for stdin) |
| `--output` | `-o` | Output directory for generated reproducer |
| `--product` | `-p` | Override detected product (e.g., `EAP`, `DG`, `JWS`) |
| `--version` | `-v` | Override detected product version |
| `--template` | `-t` | Force a specific template |
| `--format` | `-f` | Output format: `project` (default), `zip`, `tar.gz` |
| `--verbose` | | Enable verbose logging |
| `--dry-run` | | Show what would be generated without writing files |
| `--no-validate` | | Skip post-generation validation |
| `--openshift` | | Force OpenShift-style reproducer |
| `--local` | | Force local/VM-style reproducer |
| `--jdk` | | Override JDK version (11, 17, 21) |
| `--model` | | Override the AI model (default: claude-sonnet-4-20250514) |

---

## Architecture

The agent processes a customer case through a six-stage pipeline:

```
  Customer Case Input
         |
         v
  +----------------+
  | 1. Parse       |  Extract structured fields from free-form text
  +----------------+
         |
         v
  +----------------+
  | 2. Analyze     |  AI-powered issue classification, root cause hypothesis
  +----------------+
         |
         v
  +----------------+
  | 3. Strategize  |  Select reproducer type, templates, and components
  +----------------+
         |
         v
  +----------------+
  | 4. Generate    |  Produce code, config, scripts from templates + AI
  +----------------+
         |
         v
  +----------------+
  | 5. Validate    |  Build check, config syntax, container startup
  +----------------+
         |
         v
  +----------------+
  | 6. Package     |  Write output directory or archive
  +----------------+
```

**Stage details:**

1. **Parse** -- Extracts product, version, JDK, OS, error messages, stack traces, configuration snippets, and reproduction steps from the input text. Handles structured, free-form, and mixed formats.

2. **Analyze** -- Sends the parsed case to the AI model for classification. Identifies the issue category (deployment, clustering, security, performance, etc.), formulates a root cause hypothesis, and determines which product subsystems are involved.

3. **Strategize** -- Based on the analysis, selects the appropriate reproducer type (local standalone, HA cluster, OpenShift, etc.), picks base templates, and determines which components are needed (application code, server config, JGroups stack, load balancer config, etc.).

4. **Generate** -- Combines templates with AI-generated code to produce a complete reproducer. The AI fills in application-specific logic, configuration values, and test scripts. All generated code is minimal -- just enough to trigger the reported issue.

5. **Validate** -- Runs automated checks on the generated reproducer: Maven build (if applicable), XML syntax validation for configuration files, Dockerfile lint, and optionally starts containers to verify they come up cleanly.

6. **Package** -- Writes the final output to the specified directory or archive format. Includes a README specific to the reproducer with exact steps to run it.

---

## EAP 7 vs EAP 8: javax/jakarta Namespace Handling

EAP 8 moved from Java EE (javax.*) to Jakarta EE (jakarta.*) namespaces. The agent handles this automatically:

| Aspect | EAP 7.x | EAP 8.x |
|--------|---------|---------|
| Servlet API | `javax.servlet.*` | `jakarta.servlet.*` |
| CDI | `javax.inject.*`, `javax.enterprise.*` | `jakarta.inject.*`, `jakarta.enterprise.*` |
| JPA | `javax.persistence.*` | `jakarta.persistence.*` |
| JAX-RS | `javax.ws.rs.*` | `jakarta.ws.rs.*` |
| EJB | `javax.ejb.*` | `jakarta.ejb.*` |
| JMS | `javax.jms.*` | `jakarta.jms.*` |
| Maven BOM | `org.jboss.bom:jboss-eap-jakartaee8` | `org.jboss.bom:jboss-eap-ee10` |
| `web.xml` schema | Java EE 8 | Jakarta EE 10 |
| `jboss-deployment-structure.xml` | Same structure | Same structure |

When generating a reproducer:

- **EAP 7.x cases**: The agent uses `javax.*` imports, Java EE 8 dependencies, and EAP 7 BOMs.
- **EAP 8.x cases**: The agent uses `jakarta.*` imports, Jakarta EE 10 dependencies, and EAP 8 BOMs.
- **Migration cases** (EAP 7 to 8): The agent may generate both versions to demonstrate the namespace issue, and includes a `jboss-deployment-structure.xml` showing how to handle mixed-namespace third-party JARs.

If the customer case involves a third-party library that still uses `javax.*` on EAP 8, the reproducer includes a minimal JAR that simulates the problematic dependency.

---

## Security

The agent follows strict security practices in all generated output:

- **No customer secrets** -- API keys, passwords, database credentials, and other sensitive data from the case description are replaced with clearly labeled placeholders (e.g., `REPLACE_WITH_DB_PASSWORD`).
- **Self-signed certificates** -- TLS/SSL reproducers generate fresh self-signed certificates via `keytool` or `openssl` in the setup script. No real certificates are embedded.
- **Placeholder credentials** -- Default usernames and passwords in generated configuration use obvious test values (e.g., `admin` / `reproducer-password-change-me`).
- **No external network calls** -- Reproducers are designed to run in isolated environments. No callbacks to external services unless the issue specifically involves external connectivity.
- **Sanitized stack traces** -- Customer-specific class names and packages are preserved (they are needed to reproduce the issue), but file paths and hostnames from the customer environment are generalized.

---

## Templates

The agent includes built-in templates for common reproducer patterns. Templates provide the skeleton; the AI fills in issue-specific details.

### Available templates

| Template | Description |
|----------|-------------|
| `eap-standalone` | Single EAP node, standalone.xml |
| `eap-standalone-ha` | Two EAP nodes, standalone-ha.xml, JGroups TCP |
| `eap-domain` | Domain mode with host controller and server groups |
| `eap-clustering` | HA cluster with session replication and load balancer |
| `eap-elytron` | Elytron security domain configuration |
| `eap-tls` | TLS/SSL setup with keystore generation |
| `eap-datasource` | Datasource configuration with test database |
| `eap-messaging` | ActiveMQ Artemis messaging configuration |
| `eap-openshift` | EAP on OpenShift with S2I build |
| `dg-local` | Data Grid local cluster (embedded or server mode) |
| `dg-openshift` | Data Grid on OpenShift (Operator-based) |
| `dg-crosssite` | Data Grid cross-site replication |
| `jws-httpd` | JBoss Web Server / Apache httpd |
| `jws-modcluster` | mod_cluster load balancing |
| `jws-modjk` | mod_jk load balancing |

### Custom templates

To add a custom template, create a directory under `templates/custom/`:

```
templates/custom/my-template/
  template.yaml          # Template metadata and variable definitions
  pom.xml.j2             # Jinja2 template for pom.xml
  src/                   # Source templates
  config/                # Configuration templates
  scripts/               # Script templates
```

The `template.yaml` file defines variables that the AI can fill in:

```yaml
name: my-template
description: Custom reproducer template
product: EAP
min_version: "7.4"
variables:
  - name: app_name
    description: Application name
    default: reproducer-app
  - name: cache_mode
    description: Infinispan cache mode
    default: DIST_SYNC
    choices: [LOCAL, REPL_SYNC, REPL_ASYNC, DIST_SYNC, DIST_ASYNC]
```

List available templates:

```bash
python reproduce.py list-templates
```

---

## Configuration

### Environment variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | -- | Anthropic API key for AI analysis |
| `REPRODUCER_MODEL` | No | `claude-sonnet-4-20250514` | AI model to use |
| `REPRODUCER_TEMPLATE_DIR` | No | `./templates` | Path to template directory |
| `REPRODUCER_CACHE_DIR` | No | `~/.cache/reproducer-agent` | Cache directory for downloaded artifacts |
| `REPRODUCER_LOG_LEVEL` | No | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `REPRODUCER_MAX_TOKENS` | No | `8192` | Maximum tokens for AI generation |
| `JAVA_HOME` | No | System default | JDK installation for validation |
| `MAVEN_HOME` | No | System default | Maven installation for validation |
| `PODMAN_CMD` | No | `podman` | Container runtime command (podman or docker) |

### Configuration file

You can also create a `~/.reproducer-agent.yaml` configuration file:

```yaml
model: claude-sonnet-4-20250514
template_dir: ./templates
cache_dir: ~/.cache/reproducer-agent
log_level: INFO
max_tokens: 8192

defaults:
  product: EAP
  jdk: 17
  format: project

validation:
  build: true
  syntax_check: true
  container_start: false

output:
  include_readme: true
  include_docker_compose: true
  include_openshift: false
```

---

## Troubleshooting

### "ANTHROPIC_API_KEY not set"

Set the environment variable before running:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Or pass it inline:

```bash
ANTHROPIC_API_KEY="sk-ant-..." python reproduce.py generate -i case.txt -o ./output
```

### "Could not detect product from case description"

The case description may be too vague. Use the `-p` flag to specify the product:

```bash
python reproduce.py generate -i case.txt -p EAP -v 7.4 -o ./output
```

### "Maven build failed during validation"

Ensure `JAVA_HOME` and Maven are correctly configured:

```bash
java -version    # Should show JDK 11, 17, or 21
mvn -version     # Should show Maven 3.8+
```

If you do not have Maven installed, skip validation:

```bash
python reproduce.py generate -i case.txt -o ./output --no-validate
```

### "Template not found: <name>"

List available templates to find the correct name:

```bash
python reproduce.py list-templates
```

Custom templates must be placed in the configured template directory (default: `./templates/custom/`).

### "Container runtime not found"

For Docker Compose-based reproducers, ensure Docker or Podman is installed. To use Podman instead of Docker:

```bash
export PODMAN_CMD=podman
```

Or skip container validation:

```bash
python reproduce.py generate -i case.txt -o ./output --no-validate
```

### "Rate limit exceeded"

The agent makes multiple API calls during analysis and generation. If you hit rate limits, wait a few minutes and retry, or reduce `REPRODUCER_MAX_TOKENS`.

### Reproducer does not match the customer's issue

1. Check that the case description includes enough detail (error messages, stack traces, configuration).
2. Try adding more context to the case file.
3. Use `--verbose` to see the agent's analysis and understand its reasoning.
4. Use `analyze` first to review the classification before generating.

---

## License

Copyright Red Hat, Inc. Internal use only.
