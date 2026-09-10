package com.redhat.reproducer;

import java.util.logging.Logger;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;

@Path("/test")
public class TestEndpoint {
    private static final Logger LOG = Logger.getLogger(TestEndpoint.class.getName());

    @GET
    @Produces(MediaType.APPLICATION_JSON)
    public Response getInfo() {
        String nodeName = System.getProperty("jboss.node.name", "unknown");
        String json = String.format("{\"status\":\"ok\",\"node\":\"%s\",\"timestamp\":%d}",
                nodeName, System.currentTimeMillis());
        LOG.info("GET /test from node " + nodeName);
        return Response.ok(json).build();
    }

    @POST
    @Consumes(MediaType.APPLICATION_JSON)
    @Produces(MediaType.APPLICATION_JSON)
    public Response echo(String body) {
        LOG.info("POST /test received: " + body);
        return Response.ok(body).build();
    }

    @GET
    @Path("/health")
    @Produces(MediaType.TEXT_PLAIN)
    public Response health() {
        return Response.ok("UP").build();
    }
}
