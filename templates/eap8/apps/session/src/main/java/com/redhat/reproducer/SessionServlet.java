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

@WebServlet("/session")
public class SessionServlet extends HttpServlet {
    private static final Logger LOG = Logger.getLogger(SessionServlet.class.getName());

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        String delayParam = req.getParameter("delay");
        if (delayParam != null) {
            try {
                Thread.sleep(Long.parseLong(delayParam));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }

        HttpSession session = req.getSession(true);
        Integer counter = (Integer) session.getAttribute("counter");
        counter = (counter == null) ? 1 : counter + 1;
        session.setAttribute("counter", counter);

        String nodeName = System.getProperty("jboss.node.name", "unknown");
        LOG.info("Session " + session.getId() + " counter=" + counter + " node=" + nodeName);

        resp.setContentType("text/plain");
        PrintWriter out = resp.getWriter();
        out.println("Session ID: " + session.getId());
        out.println("Counter: " + counter);
        out.println("Node: " + nodeName);
        out.println("New Session: " + session.isNew());
    }

    @Override
    protected void doPost(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        HttpSession session = req.getSession(true);
        String key = req.getParameter("key");
        String value = req.getParameter("value");
        if (key != null) {
            session.setAttribute(key, value);
            resp.getWriter().println("Stored " + key + " in session " + session.getId());
        }
    }
}
