"""Offline contract tests for the Hermes Router web provider.

Run: ``python tests/test_provider.py`` (also collects under pytest).

Everything here is about the interface Hermes itself uses — the two fixed
envelopes, the offline availability check, per-URL error documents, and
``register(ctx)`` — driven through a real HTTP mock router, so the urllib client
and the JSON mapping are exercised, not stubbed out.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# A proxy env var must not swallow a loopback mock server.
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

from hermes_router_hermes import register  # noqa: E402
from hermes_router_hermes.config import load_config, normalize_base_url  # noqa: E402
from hermes_router_hermes.provider import PROVIDER_NAME, HermesRouterWebProvider  # noqa: E402

STATE: dict = {"routes": {}, "requests": []}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}
        STATE["requests"].append({"path": self.path, "body": body, "auth": self.headers.get("authorization")})
        responder = STATE["routes"].get(self.path)
        if responder is None:
            self.send_json(404, {"error": {"message": f"no route for {self.path}"}})
            return
        responder(self, body)

    def send_json(self, status: int, payload) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_raw(self, status: int, text: str, content_type: str = "text/html") -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # keep the test output readable
        return


def search_ok(handler, body):
    handler.send_json(200, {
        "provider": "tavily",
        "query": body.get("query"),
        "results": [
            {"title": "T1", "url": "https://one.example/a", "snippet": "S1", "position": 1, "score": 0.9},
            {"title": "T2", "url": "https://two.example/b", "content": "fuller body", "position": 2},
            {"title": "no url", "snippet": "dropped"},
            "not-an-object",
        ],
        "usage": {"queries_used": 1, "search_cost_usd": 0.008},
        "metrics": {"response_time_ms": 850},
    })


def fetch_ok(handler, body):
    handler.send_json(200, {
        "provider": "jina-reader",
        "url": body.get("url"),
        "title": "Example Domain",
        "content": {"format": body.get("format", "markdown"), "text": "# Example\n\nbody text", "length": 20},
        "metadata": {"author": None, "language": "en"},
        "usage": {"fetch_cost_usd": 0},
    })


class ProviderTest(unittest.TestCase):
    server: ThreadingHTTPServer
    base_url: str

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        STATE["routes"] = {"/v1/search": search_ok, "/v1/web/fetch": fetch_ok}
        STATE["requests"] = []
        self._env = dict(os.environ)
        os.environ["HERMES_ROUTER_URL"] = self.base_url
        os.environ.pop("HERMES_ROUTER_KEY", None)
        os.environ.pop("HERMES_ROUTER_SEARCH_MODEL", None)
        os.environ.pop("HERMES_ROUTER_FETCH_MODEL", None)
        os.environ.pop("HERMES_ROUTER_MAX_CHARACTERS", None)
        os.environ.pop("HERMES_ROUTER_FETCH_TIMEOUT_S", None)
        self.provider = HermesRouterWebProvider()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    # ── identity / availability ────────────────────────────────────────────
    def test_declares_the_name_hermes_wires_by(self):
        self.assertEqual(self.provider.name, "hermes-router")
        self.assertEqual(PROVIDER_NAME, self.provider.name)
        self.assertEqual(self.provider.display_name, "Hermes Router")
        self.assertTrue(self.provider.supports_search())
        self.assertTrue(self.provider.supports_extract())

    def test_is_available_makes_no_request(self):
        self.assertTrue(self.provider.is_available())
        self.assertEqual(STATE["requests"], [], "is_available() ran on every tools paint — it must stay offline")

    def test_unset_url_is_unavailable_and_says_so(self):
        os.environ.pop("HERMES_ROUTER_URL", None)
        provider = HermesRouterWebProvider()
        self.assertFalse(provider.is_available())
        search = provider.search("anything")
        self.assertFalse(search["success"])
        self.assertIn("HERMES_ROUTER_URL", search["error"])
        extract = provider.extract(["https://example.com"])
        self.assertFalse(extract["success"], "an unconfigured backend is not a per-URL failure")

    def test_base_url_env_alias_matches_the_model_provider_plugin(self):
        os.environ.pop("HERMES_ROUTER_URL", None)
        os.environ["HERMES_ROUTER_BASE_URL"] = self.base_url
        provider = HermesRouterWebProvider()
        self.assertTrue(provider.is_available(), "HERMES_ROUTER_BASE_URL is the sibling model-provider's slot")
        self.assertEqual(provider.config.base_url, self.base_url + "/v1")
        provider.search("q")
        self.assertEqual(STATE["requests"][-1]["path"], "/v1/search")

    def test_base_url_spellings_are_one_url(self):
        for raw in ("http://host:20128", "http://host:20128/", "http://host:20128/v1", "host:20128"):
            self.assertEqual(normalize_base_url(raw), "http://host:20128/v1")
        self.assertEqual(normalize_base_url("https://router.example.com/v1/"), "https://router.example.com/v1")

    # ── search ─────────────────────────────────────────────────────────────
    def test_search_envelope_and_defaults(self):
        result = self.provider.search("hermes router")
        self.assertEqual(set(result.keys()), {"success", "data"})
        web = result["data"]["web"]
        self.assertEqual(len(web), 2, "entries without a url and non-objects are dropped")
        self.assertEqual(set(web[0].keys()), {"title", "url", "description", "position"})
        self.assertEqual(web[0]["description"], "S1")
        self.assertEqual(web[1]["description"], "fuller body", "content is the fallback description")
        self.assertEqual([item["position"] for item in web], [1, 2])
        sent = STATE["requests"][-1]
        self.assertEqual(sent["path"], "/v1/search")
        self.assertEqual(sent["body"], {"model": "auto", "query": "hermes router", "max_results": 5})
        self.assertIsNone(sent["auth"], "no key was configured, so none may be sent")

    def test_search_clamps_and_forwards_limit(self):
        self.provider.search("q", limit=99)
        self.assertEqual(STATE["requests"][-1]["body"]["max_results"], 20)
        self.provider.search("q", limit=0)
        self.assertEqual(STATE["requests"][-1]["body"]["max_results"], 5)
        os.environ["HERMES_ROUTER_SEARCH_MODEL"] = "search-combo"
        self.provider.search("q", limit=3)
        self.assertEqual(STATE["requests"][-1]["body"]["model"], "search-combo")

    def test_search_sends_the_key_when_configured(self):
        os.environ["HERMES_ROUTER_KEY"] = "sk-test"
        HermesRouterWebProvider().search("q")
        self.assertEqual(STATE["requests"][-1]["auth"], "Bearer sk-test")

    def test_search_accepts_a_data_wrapped_payload(self):
        STATE["routes"]["/v1/search"] = lambda h, b: h.send_json(200, {"data": {"results": [
            {"title": "Wrapped", "url": "https://wrapped.example", "snippet": "w", "position": 1},
        ]}})
        result = self.provider.search("q")
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["web"][0]["title"], "Wrapped")

    def test_search_surfaces_router_errors(self):
        STATE["routes"]["/v1/search"] = lambda h, b: h.send_json(401, {"error": {"message": "Invalid API key"}})
        result = self.provider.search("q")
        self.assertFalse(result["success"])
        self.assertIn("Invalid API key", result["error"])

    def test_search_reports_a_non_json_body_instead_of_crashing(self):
        STATE["routes"]["/v1/search"] = lambda h, b: h.send_raw(200, "<html>gateway</html>")
        result = self.provider.search("q")
        self.assertFalse(result["success"])
        self.assertIn("not a JSON object", result["error"])

    def test_search_reports_zero_usable_results(self):
        STATE["routes"]["/v1/search"] = lambda h, b: h.send_json(200, {"provider": "brave", "results": []})
        result = self.provider.search("q")
        self.assertFalse(result["success"])
        self.assertIn("0 usable results", result["error"])

    def test_search_without_query_is_refused_before_any_request(self):
        result = self.provider.search("   ")
        self.assertFalse(result["success"])
        self.assertEqual(STATE["requests"], [])

    # ── extract ────────────────────────────────────────────────────────────
    def test_extract_envelope(self):
        result = self.provider.extract(["https://example.com"])
        self.assertEqual(set(result.keys()), {"success", "data"})
        document = result["data"][0]
        self.assertEqual(document["url"], "https://example.com")
        self.assertEqual(document["title"], "Example Domain")
        self.assertEqual(document["content"], "# Example\n\nbody text")
        self.assertEqual(document["raw_content"], document["content"])
        self.assertEqual(document["metadata"], {"author": None, "language": "en"})
        sent = STATE["requests"][-1]
        self.assertEqual(sent["path"], "/v1/web/fetch")
        self.assertEqual(sent["body"], {"model": "auto", "url": "https://example.com", "format": "markdown"})

    def test_extract_forwards_format_and_caps(self):
        os.environ["HERMES_ROUTER_MAX_CHARACTERS"] = "4000"
        self.provider.extract(["https://example.com"], format="html")
        self.assertEqual(STATE["requests"][-1]["body"]["format"], "html")
        self.assertEqual(STATE["requests"][-1]["body"]["max_characters"], 4000)
        self.provider.extract(["https://example.com"], format="nonsense")
        self.assertEqual(STATE["requests"][-1]["body"]["format"], "markdown")
        self.provider.extract(["https://example.com"], max_tokens=250)
        self.assertEqual(STATE["requests"][-1]["body"]["max_characters"], 1000, "documented 4 chars/token")
        self.provider.extract(["https://example.com"], max_characters=99)
        self.assertEqual(STATE["requests"][-1]["body"]["max_characters"], 99, "an explicit cap wins over the env")

    def test_extract_keeps_one_document_per_url_in_order(self):
        def route(handler, body):
            if body["url"].endswith("/bad"):
                handler.send_json(503, {"error": {"message": "all fetch providers failed"}})
            else:
                fetch_ok(handler, body)

        STATE["routes"]["/v1/web/fetch"] = route
        result = self.provider.extract(["https://ok.example", "https://bad.example/x/bad", "https://ok2.example"])
        self.assertTrue(result["success"])
        documents = result["data"]
        self.assertEqual(len(documents), 3)
        self.assertEqual([d["url"] for d in documents],
                         ["https://ok.example", "https://bad.example/x/bad", "https://ok2.example"])
        self.assertEqual(documents[0]["content"], "# Example\n\nbody text")
        self.assertNotIn("error", documents[0])
        self.assertIn("all fetch providers failed", documents[1]["error"])
        self.assertEqual(documents[1]["content"], "")
        self.assertEqual(documents[2]["content"], "# Example\n\nbody text", "one bad URL must not fail the call")

    def test_extract_reports_an_empty_body_as_a_url_error(self):
        STATE["routes"]["/v1/web/fetch"] = lambda h, b: h.send_json(200, {"provider": "jina-reader", "url": b["url"], "title": "T"})
        document = self.provider.extract(["https://example.com"])["data"][0]
        self.assertIn("no extractable content", document["error"])

    def test_extract_rejects_an_empty_url_list(self):
        result = self.provider.extract([])
        self.assertFalse(result["success"])

    def test_extract_times_out_as_a_url_error(self):
        os.environ["HERMES_ROUTER_FETCH_TIMEOUT_S"] = "1"

        def slow(handler, body):
            time.sleep(2.5)
            fetch_ok(handler, body)

        STATE["routes"]["/v1/web/fetch"] = slow
        started = time.time()
        result = self.provider.extract(["https://slow.example"])
        self.assertLess(time.time() - started, 2.4, "the plugin's own timeout must fire first")
        self.assertTrue(result["success"])
        self.assertIn("error", result["data"][0])

    # ── registration + diagnostics ─────────────────────────────────────────
    def test_register_puts_the_provider_in_the_registry(self):
        class FakeContext:
            def __init__(self):
                self.providers = []
                self.cli = []
                self.commands = []

            def register_web_search_provider(self, provider):
                self.providers.append(provider)

            def register_cli_command(self, **kwargs):
                self.cli.append(kwargs)

            def register_command(self, **kwargs):
                self.commands.append(kwargs)

        ctx = FakeContext()
        register(ctx)
        self.assertEqual(len(ctx.providers), 1)
        self.assertEqual(ctx.providers[0].name, "hermes-router")
        self.assertTrue(ctx.providers[0].supports_search())
        self.assertTrue(ctx.providers[0].supports_extract())
        self.assertEqual(ctx.cli[0]["name"], "hermes-router")
        self.assertTrue(callable(ctx.cli[0]["handler_fn"]))
        self.assertEqual(ctx.commands[0]["name"], "hermes-router-status")

    def test_register_tolerates_a_host_without_the_optional_surfaces(self):
        class MinimalContext:
            def __init__(self):
                self.providers = []

            def register_web_search_provider(self, provider):
                self.providers.append(provider)

        ctx = MinimalContext()
        register(ctx)
        self.assertEqual(len(ctx.providers), 1)

    def test_status_and_doctor_are_offline_and_actionable(self):
        status = self.provider.status()
        self.assertTrue(status["configured"])
        self.assertEqual(status["search_model"], "auto")
        report = self.provider.doctor(live=False)
        self.assertTrue(report["ok"])
        self.assertEqual(STATE["requests"], [], "doctor without --live must not spend quota")

    def test_doctor_live_runs_one_search_and_one_fetch(self):
        report = self.provider.doctor(live=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual([r["path"] for r in STATE["requests"]], ["/v1/search", "/v1/web/fetch"])

    def test_config_defaults(self):
        config = load_config()
        self.assertEqual(config.fetch_model, "auto")
        self.assertEqual(config.search_timeout_s, 30.0)
        self.assertEqual(config.fetch_timeout_s, 110.0)
        self.assertEqual(config.endpoints()["fetch"], self.base_url + "/v1/web/fetch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
