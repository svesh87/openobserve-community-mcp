from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openobserve_mcp import cli
from openobserve_mcp.transport import AUTH_TOKEN_ENV, STDIO, STREAMABLE_HTTP, TransportConfig

TOKEN = "token-under-test"


class ParserTests(unittest.TestCase):
    def test_serve_defaults_to_stdio(self) -> None:
        args = cli.build_parser().parse_args(["serve"])

        self.assertEqual(args.transport, STDIO)
        self.assertIsNone(args.address)

    def test_serve_accepts_the_http_transport(self) -> None:
        args = cli.build_parser().parse_args(
            ["serve", "--transport", STREAMABLE_HTTP, "--address", "0.0.0.0:8821"]
        )

        self.assertEqual(args.transport, STREAMABLE_HTTP)
        self.assertEqual(args.address, "0.0.0.0:8821")

    def test_serve_rejects_an_unknown_transport(self) -> None:
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["serve", "--transport", "sse"])


class ServeTests(unittest.TestCase):
    def test_bare_invocation_serves_stdio(self) -> None:
        # This is how an agent-started client runs the package, and it has to keep working.
        with patch("openobserve_mcp.cli.serve_main", return_value=0) as serve_main:
            self.assertEqual(cli.main([]), 0)

        serve_main.assert_called_once_with()

    def test_explicit_stdio_serves_stdio(self) -> None:
        with patch("openobserve_mcp.cli.serve_main", return_value=0) as serve_main:
            self.assertEqual(cli.main(["serve", "--transport", STDIO]), 0)

        serve_main.assert_called_once_with()

    def test_http_transport_is_served_with_the_validated_config(self) -> None:
        captured: dict[str, TransportConfig] = {}

        def fake_serve_http(server, config):
            captured["config"] = config
            captured["server"] = server
            return 0

        with patch("openobserve_mcp.cli.create_server", return_value="server"), patch(
            "openobserve_mcp.cli.serve_http", side_effect=fake_serve_http
        ), patch.dict("os.environ", {AUTH_TOKEN_ENV: TOKEN}, clear=False):
            exit_code = cli.main(
                ["serve", "--transport", STREAMABLE_HTTP, "--address", "127.0.0.1:8821"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["server"], "server")
        self.assertEqual(captured["config"].transport, STREAMABLE_HTTP)
        self.assertEqual((captured["config"].host, captured["config"].port), ("127.0.0.1", 8821))
        self.assertEqual(captured["config"].auth_token, TOKEN)

    def test_http_without_a_token_exits_with_a_message(self) -> None:
        # A traceback would read as a broken server; this is a misconfiguration.
        with patch("openobserve_mcp.cli.create_server", return_value="server"), patch(
            "openobserve_mcp.cli.serve_http"
        ) as serve_http, patch.dict("os.environ", {AUTH_TOKEN_ENV: ""}, clear=False):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["serve", "--transport", STREAMABLE_HTTP, "--address", "127.0.0.1:8821"])

        self.assertIn(AUTH_TOKEN_ENV, str(raised.exception))
        serve_http.assert_not_called()

    def test_http_without_an_address_exits_with_a_message(self) -> None:
        with patch("openobserve_mcp.cli.create_server", return_value="server"), patch(
            "openobserve_mcp.cli.serve_http"
        ) as serve_http, patch.dict("os.environ", {AUTH_TOKEN_ENV: TOKEN}, clear=False):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["serve", "--transport", STREAMABLE_HTTP])

        self.assertIn("--address", str(raised.exception))
        serve_http.assert_not_called()


class ConfigCommandTests(unittest.TestCase):
    def test_config_path_prints_the_default_path(self) -> None:
        with patch("openobserve_mcp.cli.default_config_path", return_value=Path("/tmp/config.env")):
            with patch("builtins.print") as printed:
                self.assertEqual(cli.main(["config-path"]), 0)

        printed.assert_called_once_with(Path("/tmp/config.env"))

    def test_init_config_writes_a_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "nested" / "config.env"

            with patch("builtins.print"):
                self.assertEqual(cli.main(["init-config", "--path", str(target)]), 0)

            self.assertIn("OO_BASE_URL", target.read_text(encoding="utf-8"))

            # Without --force an existing file is left alone: it may hold real credentials.
            with self.assertRaises(SystemExit):
                cli.main(["init-config", "--path", str(target)])

            target.write_text("keep me", encoding="utf-8")
            with patch("builtins.print"):
                self.assertEqual(cli.main(["init-config", "--path", str(target), "--force"]), 0)

            self.assertIn("OO_BASE_URL", target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
