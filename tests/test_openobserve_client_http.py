"""Client tests against a real local HTTP server.

The existing client tests replace request_json with a fake, which leaves the urllib path
itself unchecked: the URL that gets built, the Authorization header, and what an HTTP
error turns into. A throwaway server on a loopback port covers exactly that, and needs no
new dependency.
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openobserve_mcp.config import OpenObserveConfig
from openobserve_mcp.openobserve_client import OpenObserveApiError, OpenObserveClient


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self._respond()

    def do_POST(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        self._respond()

    def log_message(self, *args: object) -> None:
        """Keep the test output readable."""

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""

        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "body": body,
                "authorization": self.headers.get("Authorization", ""),
                "user_agent": self.headers.get("User-Agent", ""),
            }
        )

        status, payload = self.server.reply  # type: ignore[attr-defined]
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ClientHttpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.requests = []  # type: ignore[attr-defined]
        self.server.reply = (200, json.dumps({"ok": True}))  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def client(self, **overrides: object) -> OpenObserveClient:
        settings = {
            "base_url": self.base_url,
            "org_id": "default",
            "auth_mode": "basic",
            "username": "user",
            "password": "secret",
            "token": None,
            "timeout_seconds": 5.0,
            "verify_ssl": True,
        }
        settings.update(overrides)

        return OpenObserveClient(OpenObserveConfig(**settings))  # type: ignore[arg-type]

    @property
    def last_request(self) -> dict[str, str]:
        return self.server.requests[-1]  # type: ignore[attr-defined]


class RequestShapeTests(ClientHttpTestCase):
    def test_list_streams_builds_org_path_and_query(self) -> None:
        payload = self.client().list_streams(stream_type="logs", limit=5)

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(self.last_request["method"], "GET")
        self.assertTrue(self.last_request["path"].startswith("/api/default/streams?"))
        for expected in ("type=logs", "limit=5", "sort=name"):
            self.assertIn(expected, self.last_request["path"])

    def test_basic_auth_header_is_built_from_credentials(self) -> None:
        self.client().list_streams(stream_type="logs")

        # base64 of "user:secret"; the header is what OpenObserve accepts for a service
        # account, whose token goes in as the password.
        self.assertEqual(self.last_request["authorization"], "Basic dXNlcjpzZWNyZXQ=")
        self.assertIn("openobserve-community-mcp/", self.last_request["user_agent"])

    def test_bearer_mode_sends_the_token(self) -> None:
        client = self.client(auth_mode="bearer", username=None, password=None, token="the-token")

        client.list_streams(stream_type="logs")

        self.assertEqual(self.last_request["authorization"], "Bearer the-token")

    def test_stream_name_is_quoted_in_the_path(self) -> None:
        self.client().get_stream_schema(stream_name="logs/nested name")

        self.assertEqual(
            self.last_request["path"], "/api/default/streams/logs%2Fnested%20name/schema"
        )

    def test_search_sql_posts_the_query_body(self) -> None:
        self.client().search_sql(
            sql='SELECT count(*) FROM "audit"', start_time=1, end_time=2, limit=7
        )

        self.assertEqual(self.last_request["method"], "POST")
        self.assertTrue(self.last_request["path"].startswith("/api/default/_search?"))

        body = json.loads(self.last_request["body"])
        self.assertEqual(body["query"]["sql"], 'SELECT count(*) FROM "audit"')
        self.assertEqual((body["query"]["start_time"], body["query"]["end_time"]), (1, 2))
        self.assertEqual(body["query"]["size"], 7)
        self.assertFalse(body["use_cache"])

    def test_search_sql_passes_an_explicit_timeout(self) -> None:
        self.client().search_sql(sql="SELECT 1", start_time=1, end_time=2, timeout=11)

        self.assertEqual(json.loads(self.last_request["body"])["timeout"], 11)

    def test_search_around_and_values_and_traces(self) -> None:
        client = self.client()

        client.search_around(stream_name="audit", key=1700000000000000, size=3, regions="eu")
        self.assertIn("/api/default/audit/_around?", self.last_request["path"])
        self.assertIn("regions=eu", self.last_request["path"])

        client.search_values(
            stream_name="audit", fields="level", start_time=1, end_time=2, keyword="err"
        )
        self.assertIn("/api/default/audit/_values?", self.last_request["path"])
        self.assertIn("keyword=err", self.last_request["path"])

        client.get_latest_traces(
            stream_name="traces", start_time=1, end_time=2, filter_query="service='api'"
        )
        self.assertIn("/api/default/traces/traces/latest?", self.last_request["path"])
        self.assertIn("filter=service", self.last_request["path"])

    def test_dashboards(self) -> None:
        client = self.client()

        client.list_dashboards()
        # No query at all rather than a bare "?": the API is picky about empty parameters.
        self.assertEqual(self.last_request["path"], "/api/default/dashboards")

        client.list_dashboards(folder="ops", title="latency", page_size=10)
        self.assertIn("folder=ops", self.last_request["path"])
        self.assertIn("pageSize=10", self.last_request["path"])

        client.get_dashboard(dashboard_id="abc def")
        self.assertEqual(self.last_request["path"], "/api/default/dashboards/abc%20def")


class ResponseHandlingTests(ClientHttpTestCase):
    def test_empty_body_becomes_none(self) -> None:
        self.server.reply = (200, "")  # type: ignore[attr-defined]

        self.assertIsNone(self.client().list_streams(stream_type="logs"))

    def test_invalid_json_is_reported_as_an_api_error(self) -> None:
        self.server.reply = (200, "not json")  # type: ignore[attr-defined]

        with self.assertRaises(OpenObserveApiError) as raised:
            self.client().list_streams(stream_type="logs")

        self.assertEqual(raised.exception.status_code, 200)

    def test_http_error_carries_status_and_body(self) -> None:
        self.server.reply = (403, json.dumps({"message": "forbidden"}))  # type: ignore[attr-defined]

        with self.assertRaises(OpenObserveApiError) as raised:
            self.client().list_streams(stream_type="logs")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertIn("forbidden", raised.exception.body or "")

    def test_unauthorized_says_the_credentials_were_rejected(self) -> None:
        self.server.reply = (401, "")  # type: ignore[attr-defined]

        with self.assertRaises(OpenObserveApiError) as raised:
            self.client().list_streams(stream_type="logs")

        self.assertEqual(raised.exception.status_code, 401)

    def test_values_filter_error_gets_the_parser_hint(self) -> None:
        # OpenObserve answers 500 for a filter its own parser cannot read, which reads as
        # a broken server unless the client says what it actually means.
        self.server.reply = (500, json.dumps({"message": "parse error"}))  # type: ignore[attr-defined]

        with self.assertRaises(OpenObserveApiError) as raised:
            self.client().search_values(
                stream_name="audit",
                fields="level",
                start_time=1,
                end_time=2,
                filter_query="level = 'error'",
            )

        self.assertIn("filter parser", str(raised.exception))

    def test_unreachable_server_becomes_status_zero(self) -> None:
        port = self.server.server_address[1]
        self._stop()
        self.addCleanup(lambda: None)

        client = self.client(base_url=f"http://127.0.0.1:{port}")

        with self.assertRaises(OpenObserveApiError) as raised:
            client.list_streams(stream_type="logs")

        self.assertEqual(raised.exception.status_code, 0)


class OrgResolutionTests(ClientHttpTestCase):
    def test_org_is_inferred_when_not_configured(self) -> None:
        self.server.reply = (  # type: ignore[attr-defined]
            200,
            json.dumps({"data": [{"identifier": "inferred"}]}),
        )

        client = self.client(org_id=None)

        client.list_streams(stream_type="logs")

        paths = [entry["path"] for entry in self.server.requests]  # type: ignore[attr-defined]
        self.assertEqual(paths[0], "/api/organizations")
        self.assertTrue(paths[1].startswith("/api/inferred/streams?"))

        # Resolved once and cached: every tool call would otherwise pay for it.
        client.list_streams(stream_type="logs")
        self.assertEqual(
            [entry["path"] for entry in self.server.requests].count("/api/organizations"),  # type: ignore[attr-defined]
            1,
        )


class SslContextTests(ClientHttpTestCase):
    def test_verification_off_builds_a_permissive_context(self) -> None:
        client = self.client(verify_ssl=False)

        context = client._ssl_context()

        self.assertIsNotNone(context)
        self.assertFalse(context.check_hostname)

    def test_verification_on_uses_the_default_context(self) -> None:
        self.assertIsNone(self.client()._ssl_context())


if __name__ == "__main__":
    unittest.main()
