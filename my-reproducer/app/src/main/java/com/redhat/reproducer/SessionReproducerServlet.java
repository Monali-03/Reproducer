package com.redhat.reproducer;

import java.io.IOException;
import java.io.PrintWriter;
import java.io.Serializable;
import java.util.logging.Logger;
import javax.servlet.ServletException;
import javax.servlet.annotation.WebServlet;
import javax.servlet.http.HttpServlet;
import javax.servlet.http.HttpServletRequest;
import javax.servlet.http.HttpServletResponse;
import javax.servlet.http.HttpSession;

@WebServlet("/session")
public class SessionReproducerServlet extends HttpServlet {

    private static final Logger LOG = Logger.getLogger(SessionReproducerServlet.class.getName());

    @Override
    protected void doGet(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        String nodeName = System.getProperty("jboss.node.name", "unknown");
        HttpSession session = req.getSession(true);

        // Handle delay parameter for concurrency testing
        String delay = req.getParameter("delay");
        if (delay != null) {
            try { Thread.sleep(Long.parseLong(delay)); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }

        // Increment session counter
        Integer counter = (Integer) session.getAttribute("counter");
        counter = (counter == null) ? 1 : counter + 1;
        session.setAttribute("counter", counter);

        // Store cart data to simulate customer scenario
        String item = req.getParameter("item");
        if (item != null) {
            String cart = (String) session.getAttribute("cart");
            cart = (cart == null) ? item : cart + "," + item;
            session.setAttribute("cart", cart);
        }

        LOG.info("Node=" + nodeName + " SessionID=" + session.getId()
                 + " Counter=" + counter + " New=" + session.isNew());

        resp.setContentType("text/plain");
        PrintWriter out = resp.getWriter();
        out.println("node=" + nodeName);
        out.println("sessionId=" + session.getId());
        out.println("counter=" + counter);
        out.println("isNew=" + session.isNew());
        out.println("cart=" + session.getAttribute("cart"));
    }

    @Override
    protected void doDelete(HttpServletRequest req, HttpServletResponse resp)
            throws ServletException, IOException {
        HttpSession session = req.getSession(false);
        if (session != null) {
            LOG.info("Invalidating session: " + session.getId());
            session.invalidate();
        }
        resp.getWriter().println("session invalidated");
    }
}
