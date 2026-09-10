"""Streamable HTTP transport with a bearer lock in front of the MCP endpoint.

The upstream project serves stdio only, which suits a client that starts its own
process. Behind a shared server the transport has to be a port, and a port on a
multi-user host needs a lock: whatever reaches it inherits the OpenObserve credentials
this process was given.

The lock is a single static token from the environment rather than OAuth, because the
client here is a local agent configuration, not a person signing in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any, Awaitable, Callable, Mapping

from .errors import OpenObserveMcpError

STDIO = "stdio"
STREAMABLE_HTTP = "streamable-http"
TRANSPORTS = (STDIO, STREAMABLE_HTTP)

#: Token that guards the HTTP transport. Read from the environment, never from a flag:
#: a flag is visible in the process list to every user of the host.
AUTH_TOKEN_ENV = "MCP_AUTH_TOKEN"

#: The MCP endpoint is FastMCP's default path; the health check sits beside it and needs
#: no token, so a container healthcheck can use it without holding a secret.
MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TransportError(OpenObserveMcpError):
    """Raised when the requested transport cannot be served as asked."""


@dataclass(frozen=True, slots=True)
class TransportConfig:
    """Validated transport settings for one server process."""

    transport: str
    host: str = ""
    port: int = 0
    auth_token: str = ""

    @classmethod
    def load(
        cls,
        *,
        transport: str = STDIO,
        address: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> "TransportConfig":
        """Validate flags and environment together, refusing anything half-configured."""
        if env is None:
            env = os.environ

        if transport not in TRANSPORTS:
            raise TransportError(
                f"Unknown transport {transport!r}. Available: {', '.join(TRANSPORTS)}."
            )

        if transport == STDIO:
            return cls(transport=STDIO)

        if not address:
            raise TransportError(f"--address is required with --transport={STREAMABLE_HTTP}.")

        host, port = _split_address(address)

        # An empty variable is an empty string, not an absent one, so an unset token would
        # otherwise leave the port open to every local process. Refusing to start is the
        # only safe reading of it.
        token = env.get(AUTH_TOKEN_ENV, "")
        if not token:
            raise TransportError(
                f"{AUTH_TOKEN_ENV} is required with --transport={STREAMABLE_HTTP}: "
                "the server refuses to serve an unguarded port."
            )

        return cls(transport=STREAMABLE_HTTP, host=host, port=port, auth_token=token)


def _split_address(address: str) -> tuple[str, int]:
    """Split ``host:port``, keeping IPv6 brackets intact."""
    host, separator, port_text = address.rpartition(":")
    if not separator or not host:
        raise TransportError(f"Address {address!r} is not host:port, for example 0.0.0.0:8821.")

    try:
        port = int(port_text)
    except ValueError as exc:
        raise TransportError(f"Port {port_text!r} in {address!r} is not a number.") from exc

    if not 1 <= port <= 65535:
        raise TransportError(f"Port {port} in {address!r} is outside 1-65535.")

    return host, port


def bearer_token(header_value: str) -> str | None:
    """Extract the token from an Authorization header value.

    The scheme is compared case-insensitively, as HTTP requires, because clients differ
    in how they spell it.
    """
    scheme, separator, token = header_value.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None

    token = token.strip()
    return token or None


class BearerGate:
    """ASGI wrapper that answers the health check and guards everything else.

    Written against ASGI directly rather than against a framework's middleware: the app
    it wraps comes from FastMCP, and the health check has to stay outside the lock while
    the MCP endpoint stays inside it.
    """

    def __init__(self, app: Any, token: str, *, health_path: str = HEALTH_PATH) -> None:
        self._app = app
        self._token = token
        self._health_path = health_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan and websocket scopes go straight through: FastMCP starts its session
        # manager in lifespan, and swallowing that would leave the server unable to answer.
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        if scope.get("path") == self._health_path:
            await _plain_text(send, 200, "ok\n")
            return

        if not self._authorized(scope):
            await _plain_text(
                send,
                401,
                "authentication required\n",
                extra_headers=[(b"www-authenticate", b'Bearer realm="openobserve-mcp"')],
            )
            return

        await self._app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers") or ():
            if name.lower() != b"authorization":
                continue
            presented = bearer_token(value.decode("latin-1"))
            # Constant time, so a wrong token gives nothing away through response timing.
            return presented is not None and compare_digest(presented, self._token)

        return False


async def _plain_text(
    send: Send,
    status: int,
    body: str,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = body.encode("utf-8")
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(payload)).encode("ascii")),
    ]
    headers.extend(extra_headers or [])

    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def build_app(server: Any, token: str) -> BearerGate:
    """Wrap a FastMCP server's streamable HTTP app in the bearer gate."""
    return BearerGate(server.streamable_http_app(), token)


def serve_http(server: Any, config: TransportConfig) -> int:
    """Serve the MCP endpoint over streamable HTTP until the process is stopped."""
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
        raise TransportError(
            "The 'uvicorn' package is required for the streamable-http transport."
        ) from exc

    uvicorn.run(build_app(server, config.auth_token), host=config.host, port=config.port)

    return 0
