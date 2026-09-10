from __future__ import annotations

import unittest

from starlette.testclient import TestClient

from openobserve_mcp.transport import (
    AUTH_TOKEN_ENV,
    HEALTH_PATH,
    MCP_PATH,
    STDIO,
    STREAMABLE_HTTP,
    BearerGate,
    TransportConfig,
    TransportError,
    bearer_token,
    build_app,
)

TOKEN = "token-under-test"


class EchoApp:
    """Stands in for the FastMCP app: records what got through the gate."""

    def __init__(self) -> None:
        self.http_calls = 0
        self.lifespan_calls = 0

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            self.lifespan_calls += 1
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
            return

        self.http_calls += 1
        body = b"reached the server\n"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class FakeServer:
    """Stands in for FastMCP, which needs real configuration to be created."""

    def __init__(self, app) -> None:
        self._app = app

    def streamable_http_app(self):
        return self._app


class TransportConfigTests(unittest.TestCase):
    def test_stdio_needs_nothing_else(self) -> None:
        config = TransportConfig.load(transport=STDIO, env={})

        self.assertEqual(config.transport, STDIO)
        self.assertEqual(config.auth_token, "")

    def test_http_reads_the_token_from_the_environment(self) -> None:
        config = TransportConfig.load(
            transport=STREAMABLE_HTTP,
            address="0.0.0.0:8821",
            env={AUTH_TOKEN_ENV: TOKEN},
        )

        self.assertEqual((config.host, config.port), ("0.0.0.0", 8821))
        self.assertEqual(config.auth_token, TOKEN)

    def test_refusals(self) -> None:
        cases = {
            "unknown transport": {"transport": "sse", "env": {}},
            "http without address": {"transport": STREAMABLE_HTTP, "env": {AUTH_TOKEN_ENV: TOKEN}},
            # An unset variable arrives as an empty string, which would otherwise leave the
            # port open to every local process.
            "http without token": {
                "transport": STREAMABLE_HTTP,
                "address": "0.0.0.0:8821",
                "env": {},
            },
            "http with empty token": {
                "transport": STREAMABLE_HTTP,
                "address": "0.0.0.0:8821",
                "env": {AUTH_TOKEN_ENV: ""},
            },
            "address without port": {
                "transport": STREAMABLE_HTTP,
                "address": "0.0.0.0",
                "env": {AUTH_TOKEN_ENV: TOKEN},
            },
            "port is not a number": {
                "transport": STREAMABLE_HTTP,
                "address": "0.0.0.0:http",
                "env": {AUTH_TOKEN_ENV: TOKEN},
            },
            "port out of range": {
                "transport": STREAMABLE_HTTP,
                "address": "0.0.0.0:70000",
                "env": {AUTH_TOKEN_ENV: TOKEN},
            },
        }

        for name, kwargs in cases.items():
            with self.subTest(name):
                with self.assertRaises(TransportError):
                    TransportConfig.load(**kwargs)


class BearerTokenTests(unittest.TestCase):
    def test_parsing(self) -> None:
        cases = [
            ("Bearer abc", "abc"),
            # HTTP says the scheme is case-insensitive, and clients do vary.
            ("bearer abc", "abc"),
            ("BEARER   abc  ", "abc"),
            ("Bearer ", None),
            ("Bearer", None),
            ("", None),
            ("Basic abc", None),
            ("abc", None),
        ]

        for header, expected in cases:
            with self.subTest(header):
                self.assertEqual(bearer_token(header), expected)


class BearerGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = EchoApp()
        self.client = TestClient(BearerGate(self.app, TOKEN))

    def test_health_answers_without_a_token(self) -> None:
        # The container healthcheck has no way to hold a secret, so this path stays bare.
        response = self.client.get(HEALTH_PATH)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text.strip(), "ok")
        self.assertEqual(self.app.http_calls, 0)

    def test_mcp_refuses_without_a_valid_token(self) -> None:
        cases = {
            "no header": {},
            "empty bearer": {"Authorization": "Bearer "},
            "wrong token": {"Authorization": "Bearer wrong-token"},
            "another scheme": {"Authorization": f"Basic {TOKEN}"},
            "token without scheme": {"Authorization": TOKEN},
        }

        for name, headers in cases.items():
            with self.subTest(name):
                response = self.client.post(MCP_PATH, headers=headers)

                self.assertEqual(response.status_code, 401)
                self.assertIn("openobserve-mcp", response.headers.get("www-authenticate", ""))

        self.assertEqual(self.app.http_calls, 0)

    def test_mcp_passes_a_valid_token_through(self) -> None:
        response = self.client.post(MCP_PATH, headers={"Authorization": f"bEaReR {TOKEN}"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("reached the server", response.text)
        self.assertEqual(self.app.http_calls, 1)

    def test_lifespan_reaches_the_wrapped_app(self) -> None:
        # FastMCP starts its session manager in lifespan; swallowing it would leave the
        # server unable to answer anything.
        with TestClient(BearerGate(self.app, TOKEN)):
            pass

        self.assertEqual(self.app.lifespan_calls, 1)

    def test_build_app_wraps_the_servers_own_app(self) -> None:
        gate = build_app(FakeServer(self.app), TOKEN)

        with TestClient(gate) as client:
            self.assertEqual(client.get(HEALTH_PATH).status_code, 200)
            self.assertEqual(client.post(MCP_PATH).status_code, 401)


if __name__ == "__main__":
    unittest.main()
