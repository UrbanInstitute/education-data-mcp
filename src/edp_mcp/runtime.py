"""Process entry point: transport selection, health route, transport security.

Transport is stdio by default, which is what local MCP clients and the tests
speak. Set MCP_TRANSPORT=streamable-http to serve over HTTP for a hosted
deployment.
"""

from __future__ import annotations

import os

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

__all__ = ["add_health_route", "serve"]


def _split_env(name: str) -> list[str]:
    """Comma-separated env var into a list, blanks dropped."""
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def _transport_security() -> TransportSecuritySettings | None:
    """Host/Origin allow-lists for DNS-rebinding protection.

    The SDK turns this protection OFF when it gets None, and rejects EVERY
    request when it gets a settings object with an empty allowed_hosts — so the
    unset case has to return None rather than an empty allow-list, or the first
    deployment answers nothing at all.

    Set MCP_ALLOWED_HOSTS to the public hostname before exposing this beyond a
    private network (MCP_ALLOWED_ORIGINS too if browser clients will connect).
    """
    hosts = _split_env("MCP_ALLOWED_HOSTS")
    origins = _split_env("MCP_ALLOWED_ORIGINS")
    if not hosts and not origins:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def add_health_route(mcp: MCPServer, server_name: str) -> None:
    """Register GET /health, separate from /mcp so load balancers can poll it.

    A container orchestrator needs a liveness probe that is a plain unauthenticated
    GET. Polling /mcp does not work: it speaks JSON-RPC over POST and answers a
    bare GET with an error, which every health checker reads as a dead task.
    """

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": server_name})


def serve(mcp: MCPServer) -> None:
    """Run `mcp` on the transport named by MCP_TRANSPORT (default stdio).

    Branching on the literal rather than forwarding the string keeps the two
    transports' arguments distinct, and turns a typo in the env var into a named
    error instead of a server that starts on the wrong transport.
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio")

    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "streamable-http":
        # host/port are run() arguments in mcp 2.x. They were Settings fields in
        # 1.x, and assigning them there now raises rather than being ignored.
        mcp.run(
            transport="streamable-http",
            # Bind all interfaces: in a container the port is published by the
            # runtime, and 127.0.0.1 is unreachable from outside the task.
            host=os.environ.get("MCP_HOST", "0.0.0.0"),
            # PORT is injected by ECS/most PaaS; 8080 for a bare EC2 run.
            port=int(os.environ.get("PORT", "8080")),
            # No cross-request state to keep: every tool call is self-contained
            # and the caches are per-process, not per-session. Stateless lets any
            # instance answer any request, so scaling out needs no affinity.
            stateless_http=True,
            # Must equal the path nginx routes on. A plain `proxy_pass` forwards
            # the URI UNCHANGED, so behind `location /mcp/edp` the server is
            # asked for /mcp/edp and answers 404 while sitting on the default
            # /mcp. Setting this instead of rewriting in nginx keeps the public
            # URL free of a redundant trailing /mcp.
            streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
            transport_security=_transport_security(),
        )
    else:
        raise SystemExit(
            f"Unknown MCP_TRANSPORT {transport!r}. Use 'stdio' (default) or "
            "'streamable-http'."
        )
