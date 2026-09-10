package com.redhat.reproducer;

import java.io.IOException;
import java.io.PrintWriter;
import java.util.logging.Logger;
import jakarta.servlet.ServletException;
import jakarta.servlet.annotation.WebServlet;
import jakarta.servlet.http.HttpServlet;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;

@WebServlet("/test")
public class TestServlet extends HttpServlet {
    private static final Logger LOG = Logger.getLogger(TestServlet.class.getName());

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        HttpSession session = req.getSession(true);
        String nodeName = System.getProperty("jboss.node.name", "unknown");

        LOG.info("Request received on node: " + nodeName + ", sessionId: " + session.getId());

        resp.setContentType("text/plain");
        PrintWriter out = resp.getWriter();
        out.println("Reproducer Servlet OK");
        out.println("Node: " + nodeName);
        out.println("Session ID: " + session.getId());
        out.println("Session New: " + session.isNew());
        out.println("Remote Addr: " + req.getRemoteAddr());
        out.println("Thread: " + Thread.currentThread().getName());
    }

    @Override
    protected void doPost(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        HttpSession session = req.getSession(true);
        String key = req.getParameter("key");
        String value = req.getParameter("value");
        if (key != null && value != null) {
            session.setAttribute(key, value);
        }
        resp.setContentType("text/plain");
        resp.getWriter().println("Stored: " + key + "=" + value + " in session " + session.getId());
    }
}
