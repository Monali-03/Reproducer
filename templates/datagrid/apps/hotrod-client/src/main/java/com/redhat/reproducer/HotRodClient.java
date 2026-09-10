package com.redhat.reproducer;

import org.infinispan.client.hotrod.RemoteCache;
import org.infinispan.client.hotrod.RemoteCacheManager;
import org.infinispan.client.hotrod.configuration.ConfigurationBuilder;
import org.infinispan.client.hotrod.configuration.SaslQop;
import org.infinispan.commons.configuration.StringConfiguration;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.TimeUnit;

/**
 * Hot Rod client application for Red Hat Data Grid reproducer testing.
 *
 * <p>Connects to a Data Grid / Infinispan server via the Hot Rod protocol
 * and performs basic CRUD operations to verify connectivity and cache behavior.</p>
 *
 * <p>Usage:
 * <pre>
 *   java -jar hotrod-client.jar [--host HOST] [--port PORT]
 *       [--cache CACHE_NAME] [--count OPERATION_COUNT]
 *       [--username USER] [--password PASS]
 * </pre>
 * </p>
 */
public class HotRodClient {

    private static final Logger LOG = LoggerFactory.getLogger(HotRodClient.class);

    // Default connection parameters
    private static final String DEFAULT_HOST = "localhost";
    private static final int DEFAULT_PORT = 11222;
    private static final String DEFAULT_CACHE = "testCache";
    private static final int DEFAULT_COUNT = 10;
    private static final String DEFAULT_USERNAME = "admin";
    private static final String DEFAULT_PASSWORD = "changeit";

    private final String host;
    private final int port;
    private final String cacheName;
    private final int operationCount;
    private final String username;
    private final String password;

    public HotRodClient(String host, int port, String cacheName, int operationCount,
                        String username, String password) {
        this.host = host;
        this.port = port;
        this.cacheName = cacheName;
        this.operationCount = operationCount;
        this.username = username;
        this.password = password;
    }

    /**
     * Build the Hot Rod client configuration.
     */
    private ConfigurationBuilder buildConfiguration() {
        ConfigurationBuilder builder = new ConfigurationBuilder();
        builder.addServer()
                   .host(host)
                   .port(port)
               .security()
                   .authentication()
                       .enable()
                       .username(username)
                       .password(password)
                       .realm("default")
                       .saslMechanism("SCRAM-SHA-512")
                       .saslQop(SaslQop.AUTH)
               .connectionPool()
                   .maxActive(10)
                   .exhaustedAction(org.infinispan.client.hotrod.configuration.ExhaustedAction.WAIT)
               .connectionTimeout(5000)
               .socketTimeout(5000);

        LOG.info("Configuration built for {}:{}", host, port);
        return builder;
    }

    /**
     * Run all CRUD operations against the cache.
     */
    public void execute() {
        LOG.info("=== Hot Rod Client - Reproducer Test ===");
        LOG.info("Connecting to Data Grid server at {}:{}", host, port);
        LOG.info("Target cache: {}", cacheName);
        LOG.info("Operation count: {}", operationCount);

        RemoteCacheManager cacheManager = null;

        try {
            // Build configuration and connect
            ConfigurationBuilder config = buildConfiguration();
            cacheManager = new RemoteCacheManager(config.build());
            LOG.info("Successfully connected to Data Grid server");

            // Get or create the cache
            RemoteCache<String, String> cache = getOrCreateCache(cacheManager);
            if (cache == null) {
                LOG.error("Failed to obtain cache '{}'. Aborting.", cacheName);
                return;
            }

            LOG.info("Cache '{}' obtained. Size before test: {}", cacheName, cache.size());

            // Execute CRUD operations
            testPut(cache);
            testGet(cache);
            testUpdate(cache);
            testRemove(cache);
            testBulkOperations(cache);
            testConditionalOperations(cache);

            LOG.info("=== All operations completed successfully ===");
            LOG.info("Final cache size: {}", cache.size());

        } catch (org.infinispan.client.hotrod.exceptions.TransportException e) {
            LOG.error("Connection failed: unable to reach server at {}:{}. " +
                      "Verify the server is running and accessible.", host, port, e);
        } catch (org.infinispan.client.hotrod.exceptions.HotRodClientException e) {
            LOG.error("Hot Rod protocol error: {}", e.getMessage(), e);
        } catch (Exception e) {
            LOG.error("Unexpected error during execution: {}", e.getMessage(), e);
        } finally {
            if (cacheManager != null) {
                try {
                    cacheManager.close();
                    LOG.info("Cache manager closed");
                } catch (Exception e) {
                    LOG.warn("Error closing cache manager: {}", e.getMessage());
                }
            }
        }
    }

    /**
     * Get existing cache or create it with a default distributed configuration.
     */
    private RemoteCache<String, String> getOrCreateCache(RemoteCacheManager cacheManager) {
        RemoteCache<String, String> cache = cacheManager.getCache(cacheName);

        if (cache == null) {
            LOG.info("Cache '{}' not found, creating with default distributed configuration", cacheName);
            String cacheConfig = String.format(
                "<distributed-cache name=\"%s\" mode=\"SYNC\" owners=\"2\">" +
                "  <encoding><key media-type=\"application/x-java-object\"/>" +
                "  <value media-type=\"application/x-java-object\"/></encoding>" +
                "</distributed-cache>", cacheName);
            cache = cacheManager.administration()
                .getOrCreateCache(cacheName, new StringConfiguration(cacheConfig));
            LOG.info("Cache '{}' created successfully", cacheName);
        }

        return cache;
    }

    /**
     * Test PUT operations.
     */
    private void testPut(RemoteCache<String, String> cache) {
        LOG.info("--- PUT Operations ---");
        long startTime = System.currentTimeMillis();

        for (int i = 0; i < operationCount; i++) {
            String key = "key-" + i;
            String value = "value-" + i + "-" + System.currentTimeMillis();
            cache.put(key, value);
            LOG.debug("PUT: {} = {}", key, value);
        }

        long elapsed = System.currentTimeMillis() - startTime;
        LOG.info("PUT {} entries in {} ms ({} ops/sec)",
                operationCount, elapsed,
                elapsed > 0 ? (operationCount * 1000L / elapsed) : "N/A");
    }

    /**
     * Test GET operations.
     */
    private void testGet(RemoteCache<String, String> cache) {
        LOG.info("--- GET Operations ---");
        long startTime = System.currentTimeMillis();
        int found = 0;
        int notFound = 0;

        for (int i = 0; i < operationCount; i++) {
            String key = "key-" + i;
            String value = cache.get(key);
            if (value != null) {
                found++;
                LOG.debug("GET: {} = {}", key, value);
            } else {
                notFound++;
                LOG.warn("GET: {} = NULL (unexpected)", key);
            }
        }

        long elapsed = System.currentTimeMillis() - startTime;
        LOG.info("GET {} entries in {} ms (found={}, notFound={})",
                operationCount, elapsed, found, notFound);
    }

    /**
     * Test UPDATE operations (put with existing keys).
     */
    private void testUpdate(RemoteCache<String, String> cache) {
        LOG.info("--- UPDATE Operations ---");
        long startTime = System.currentTimeMillis();

        for (int i = 0; i < operationCount; i++) {
            String key = "key-" + i;
            String oldValue = cache.get(key);
            String newValue = "updated-value-" + i + "-" + System.currentTimeMillis();
            String previous = cache.put(key, newValue);
            LOG.debug("UPDATE: {} old={} new={}", key, previous, newValue);
        }

        long elapsed = System.currentTimeMillis() - startTime;
        LOG.info("UPDATE {} entries in {} ms", operationCount, elapsed);
    }

    /**
     * Test REMOVE operations.
     */
    private void testRemove(RemoteCache<String, String> cache) {
        LOG.info("--- REMOVE Operations ---");
        long startTime = System.currentTimeMillis();
        int removed = 0;

        // Remove half the entries to keep some data for further testing
        for (int i = 0; i < operationCount / 2; i++) {
            String key = "key-" + i;
            String value = cache.remove(key);
            if (value != null) {
                removed++;
                LOG.debug("REMOVE: {} = {}", key, value);
            }
        }

        long elapsed = System.currentTimeMillis() - startTime;
        LOG.info("REMOVE {} entries in {} ms (removed={})",
                operationCount / 2, elapsed, removed);
    }

    /**
     * Test bulk operations: putAll, getAll, size, clear.
     */
    private void testBulkOperations(RemoteCache<String, String> cache) {
        LOG.info("--- Bulk Operations ---");

        // putAll
        java.util.Map<String, String> bulkData = new java.util.HashMap<>();
        for (int i = 0; i < operationCount; i++) {
            bulkData.put("bulk-key-" + i, "bulk-value-" + i);
        }
        long startTime = System.currentTimeMillis();
        cache.putAll(bulkData);
        LOG.info("putAll {} entries in {} ms", bulkData.size(),
                System.currentTimeMillis() - startTime);

        // size
        int size = cache.size();
        LOG.info("Cache size: {}", size);

        // Put with lifespan
        cache.put("expiring-key", "expiring-value", 30, TimeUnit.SECONDS);
        LOG.info("PUT with 30s lifespan: expiring-key");

        // containsKey
        boolean exists = cache.containsKey("bulk-key-0");
        LOG.info("containsKey('bulk-key-0'): {}", exists);
    }

    /**
     * Test conditional (CAS) operations: putIfAbsent, replace, replaceWithVersion.
     */
    private void testConditionalOperations(RemoteCache<String, String> cache) {
        LOG.info("--- Conditional Operations ---");

        // putIfAbsent
        String prev = cache.putIfAbsent("conditional-key", "first-value");
        LOG.info("putIfAbsent('conditional-key', 'first-value'): previous={}", prev);

        // putIfAbsent again (should not overwrite)
        prev = cache.putIfAbsent("conditional-key", "second-value");
        LOG.info("putIfAbsent('conditional-key', 'second-value'): previous={} (should be 'first-value')", prev);

        // replace
        boolean replaced = cache.replace("conditional-key", "first-value", "replaced-value");
        LOG.info("replace('conditional-key', 'first-value', 'replaced-value'): {}", replaced);

        // Verify replacement
        String current = cache.get("conditional-key");
        LOG.info("GET 'conditional-key': {} (should be 'replaced-value')", current);
    }

    /**
     * Parse command line arguments and run the client.
     */
    public static void main(String[] args) {
        String host = DEFAULT_HOST;
        int port = DEFAULT_PORT;
        String cacheName = DEFAULT_CACHE;
        int count = DEFAULT_COUNT;
        String username = DEFAULT_USERNAME;
        String password = DEFAULT_PASSWORD;

        // Parse command line arguments
        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--host":
                case "-h":
                    if (i + 1 < args.length) host = args[++i];
                    break;
                case "--port":
                case "-p":
                    if (i + 1 < args.length) port = Integer.parseInt(args[++i]);
                    break;
                case "--cache":
                case "-c":
                    if (i + 1 < args.length) cacheName = args[++i];
                    break;
                case "--count":
                case "-n":
                    if (i + 1 < args.length) count = Integer.parseInt(args[++i]);
                    break;
                case "--username":
                case "-u":
                    if (i + 1 < args.length) username = args[++i];
                    break;
                case "--password":
                    if (i + 1 < args.length) password = args[++i];
                    break;
                case "--help":
                    printUsage();
                    return;
                default:
                    LOG.warn("Unknown argument: {}", args[i]);
            }
        }

        HotRodClient client = new HotRodClient(host, port, cacheName, count, username, password);
        client.execute();
    }

    private static void printUsage() {
        System.out.println("Usage: java -jar hotrod-client.jar [options]");
        System.out.println();
        System.out.println("Options:");
        System.out.println("  --host, -h HOST         Data Grid server host (default: localhost)");
        System.out.println("  --port, -p PORT         Hot Rod port (default: 11222)");
        System.out.println("  --cache, -c CACHE       Cache name (default: testCache)");
        System.out.println("  --count, -n COUNT       Number of operations (default: 10)");
        System.out.println("  --username, -u USER     Authentication username (default: admin)");
        System.out.println("  --password PASS         Authentication password (default: changeit)");
        System.out.println("  --help                  Show this help message");
    }
}
