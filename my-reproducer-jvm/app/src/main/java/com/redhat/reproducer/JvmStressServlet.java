package com.redhat.reproducer;

import java.io.IOException;
import java.io.PrintWriter;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.lang.management.MemoryUsage;
import java.lang.management.ThreadMXBean;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import javax.servlet.ServletException;
import javax.servlet.annotation.WebServlet;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;

/**
 * On-demand JVM stress endpoint for reproducing EAP / Data Grid JVM issues:
 * heap OutOfMemoryError, slow memory leak, GC pressure, high CPU, and thread
 * exhaustion. Select a mode with ?mode=...; observe with ?mode=status.
 *
 *   GET /jvm-reproducer/jvm?mode=status
 *   GET /jvm-reproducer/jvm?mode=heap-oom            (allocates until OOM)
 *   GET /jvm-reproducer/jvm?mode=leak&mb=10          (leaks 10MB per call, retained)
 *   GET /jvm-reproducer/jvm?mode=gc&mb=50            (churns 50MB to stress GC)
 *   GET /jvm-reproducer/jvm?mode=cpu&threads=4&secs=20
 *   GET /jvm-reproducer/jvm?mode=threads&count=500   (parks N threads)
 *   GET /jvm-reproducer/jvm?mode=free                (drops leaked refs)
 *
 * Run the server with dump capture enabled (see run.sh):
 *   -Xmx<small> -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=...
 *   -Xlog:gc*:file=gc.log:time,uptime,level,tags
 */
@WebServlet("/jvm")
public class JvmStressServlet extends HttpServlet {

    // Static so leaked memory / threads survive across requests and are not GC'd.
    private static final List<byte[]> LEAK = new ArrayList<>();
    private static final List<Thread> PARKED = new ArrayList<>();
    private static final AtomicLong CPU_RUNS = new AtomicLong();

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        resp.setContentType("text/plain");
        PrintWriter out = resp.getWriter();
        String mode = req.getParameter("mode");
        mode = (mode == null) ? "status" : mode;
        String node = System.getProperty("jboss.node.name", "standalone");

        try {
            switch (mode) {
                case "status":     status(out, node); break;
                case "heap-oom":   heapOom(out); break;
                case "leak":       leak(out, intParam(req, "mb", 10)); break;
                case "gc":         gc(out, intParam(req, "mb", 50)); break;
                case "cpu":        cpu(out, intParam(req, "threads", 4), intParam(req, "secs", 20)); break;
                case "threads":    threads(out, intParam(req, "count", 500)); break;
                case "free":       free(out); break;
                default:           out.println("unknown mode: " + mode);
            }
        } catch (OutOfMemoryError e) {
            // Reaching here means the heap dump has already been written by the JVM.
            out.println("OutOfMemoryError: " + e.getMessage());
            out.println(">>> Heap dump written to -XX:HeapDumpPath (see run.sh). <<<");
        }
    }

    private void status(PrintWriter out, String node) {
        MemoryMXBean mem = ManagementFactory.getMemoryMXBean();
        MemoryUsage heap = mem.getHeapMemoryUsage();
        MemoryUsage nonHeap = mem.getNonHeapMemoryUsage();
        ThreadMXBean threads = ManagementFactory.getThreadMXBean();
        out.println("node=" + node);
        out.println("heapUsedMB=" + (heap.getUsed() >> 20));
        out.println("heapMaxMB=" + (heap.getMax() >> 20));
        out.println("heapPctUsed=" + pct(heap.getUsed(), heap.getMax()));
        out.println("metaspaceUsedMB=" + (nonHeap.getUsed() >> 20));
        out.println("liveThreads=" + threads.getThreadCount());
        out.println("peakThreads=" + threads.getPeakThreadCount());
        out.println("leakedChunks=" + LEAK.size());
        out.println("parkedThreads=" + PARKED.size());
        out.println("cpuRunsCompleted=" + CPU_RUNS.get());
    }

    private void heapOom(PrintWriter out) {
        out.println("Allocating 10MB chunks until OutOfMemoryError...");
        out.flush();
        int chunks = 0;
        while (true) {                       // intentional: exhaust the heap
            LEAK.add(new byte[10 * 1024 * 1024]);
            chunks++;
        }
    }

    private void leak(PrintWriter out, int mb) {
        LEAK.add(new byte[mb * 1024 * 1024]);
        out.println("leaked " + mb + "MB; totalLeakedChunks=" + LEAK.size());
        out.println("(call repeatedly to grow heap; use mode=status to watch heapPctUsed)");
    }

    private void gc(PrintWriter out, int mb) {
        long start = System.nanoTime();
        for (int i = 0; i < 200; i++) {      // allocate + drop -> GC churn
            byte[] garbage = new byte[mb * 1024];
            if (garbage[0] == 42) out.print("");   // prevent dead-code elimination
        }
        long ms = (System.nanoTime() - start) / 1_000_000;
        out.println("churned ~" + (mb * 200 / 1024) + "MB in " + ms + "ms (see gc.log for pauses)");
    }

    private void cpu(PrintWriter out, int nThreads, int secs) {
        long deadline = System.currentTimeMillis() + secs * 1000L;
        List<Thread> workers = new ArrayList<>();
        for (int i = 0; i < nThreads; i++) {
            Thread t = new Thread(() -> {
                double x = 0;
                while (System.currentTimeMillis() < deadline) { x += Math.sqrt(x + 1); }
                CPU_RUNS.incrementAndGet();
            }, "cpu-burner-" + i);
            t.setDaemon(true);
            t.start();
            workers.add(t);
        }
        out.println("started " + nThreads + " CPU-burner threads for " + secs + "s");
        out.println("capture thread dumps now: ./capture.sh  (look for cpu-burner-* RUNNABLE)");
    }

    private void threads(PrintWriter out, int count) {
        int created = 0;
        for (int i = 0; i < count; i++) {
            Thread t = new Thread(() -> {
                try { Thread.sleep(Long.MAX_VALUE); } catch (InterruptedException ignored) {}
            }, "parked-" + i);
            t.setDaemon(true);
            try { t.start(); PARKED.add(t); created++; }
            catch (OutOfMemoryError e) { break; }   // "unable to create new native thread"
        }
        out.println("created " + created + " parked threads; totalParked=" + PARKED.size());
        out.println("(raise count until you hit 'unable to create new native thread')");
    }

    private void free(PrintWriter out) {
        int chunks = LEAK.size();
        LEAK.clear();
        PARKED.forEach(Thread::interrupt);
        int parked = PARKED.size();
        PARKED.clear();
        System.gc();
        out.println("released " + chunks + " leaked chunks and " + parked + " parked threads");
    }

    private static int intParam(HttpServletRequest req, String name, int def) {
        String v = req.getParameter(name);
        try { return (v == null) ? def : Integer.parseInt(v); } catch (NumberFormatException e) { return def; }
    }

    private static String pct(long used, long max) {
        return (max <= 0) ? "n/a" : (used * 100 / max) + "%";
    }
}
