"""Contract tests for the Hermes Router model-provider declaration.

Run: ``python tests/test_profile.py`` (also collects under pytest).

Hermes is not installed here, so the `providers` module is stubbed with a
`ProviderProfile` dataclass that accepts ONLY the fields the plugin declares:
a field name Hermes does not have therefore fails the import instead of being
silently dropped, which is the failure mode this kind of plugin can actually
ship with. Everything else — the base-URL rules, the catalogue fetch, the
fallbacks — runs for real, including against a mock router over HTTP.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional, Tuple

# The model half now lives INSIDE the merged web plugin. It is loaded here by
# FILE PATH on purpose: that is exactly what the documented drop-in does when it
# is copied to $HERMES_HOME/plugins/model-providers/hermes-router/__init__.py, so
# this file keeps proving the standalone contract after the merge.
PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_FILE = PLUGIN_DIR / "hermes_router_hermes" / "model_profile.py"
sys.path.insert(0, str(PLUGIN_DIR))
os.environ["no_proxy"] = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

REGISTERED: list = []
STATE: dict = {"payload": {"object": "list", "data": [{"id": "tavily/x"}]}, "status": 200, "raw": None, "requests": []}


def _install_stub_providers() -> None:
    """Stub `providers` / `providers.base` with the documented profile fields."""

    @dataclasses.dataclass
    class ProviderProfile:
        name: str
        aliases: Tuple[str, ...] = ()
        display_name: Optional[str] = None
        description: Optional[str] = None
        signup_url: Optional[str] = None
        env_vars: Tuple[str, ...] = ()
        base_url: Optional[str] = None
        models_url: Optional[str] = None
        auth_type: str = "api_key"
        api_mode: str = "chat_completions"
        default_aux_model: Optional[str] = None
        fallback_models: Tuple[str, ...] = ()
        supports_vision: bool = False

        def fetch_models(self, **kwargs):  # pragma: no cover - overridden by the plugin
            return None

    providers = type(sys)("providers")
    providers.register_provider = lambda profile: REGISTERED.append(profile)
    base = type(sys)("providers.base")
    base.ProviderProfile = ProviderProfile
    providers.base = base
    sys.modules["providers"] = providers
    sys.modules["providers.base"] = base


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - http.server API
        STATE["requests"].append({"path": self.path, "auth": self.headers.get("authorization")})
        if STATE["status"] != 200:
            body = json.dumps({"error": {"message": "nope"}}).encode()
            self._send(STATE["status"], body, "application/json")
            return
        if STATE["raw"] is not None:
            self._send(200, STATE["raw"].encode(), "text/html")
            return
        self._send(200, json.dumps(STATE["payload"]).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def load_plugin(env: dict) -> Any:
    """Import the plugin fresh so its module-level profile reads this env."""
    REGISTERED.clear()
    for key in ("HERMES_ROUTER_URL", "HERMES_ROUTER_BASE_URL", "HERMES_ROUTER_KEY", "HERMES_ROUTER_AUX_MODEL", "HERMES_ROUTER_MODELS"):
        os.environ.pop(key, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    _install_stub_providers()
    name = "hermes_router_model_under_test_" + str(len(sys.modules))
    spec = importlib.util.spec_from_file_location(name, PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ProfileTest(unittest.TestCase):
    server: ThreadingHTTPServer
    base: str

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        STATE.update({"payload": {"object": "list", "data": [{"id": "mimo"}, {"id": "a6api/deepseek-v4.1-flash"}]},
                      "status": 200, "raw": None, "requests": []})
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    # ── the declaration itself ─────────────────────────────────────────────
    def test_registers_one_profile_with_the_documented_shape(self):
        load_plugin({})
        self.assertEqual(len(REGISTERED), 1, "exactly one profile, registered at module level")
        profile = REGISTERED[0]
        self.assertEqual(profile.name, "hermes-router")
        self.assertIn("hermesrouter", profile.aliases)
        self.assertEqual(profile.auth_type, "api_key")
        self.assertEqual(profile.api_mode, "chat_completions")
        self.assertTrue(profile.supports_vision)
        self.assertEqual(profile.display_name, "Hermes Router")
        self.assertIn("hermes-router", profile.signup_url)
        self.assertTrue(profile.description)

    def test_env_vars_declare_key_then_base_url_override(self):
        module = load_plugin({})
        profile = REGISTERED[0]
        self.assertEqual(profile.env_vars, ("HERMES_ROUTER_KEY", "HERMES_ROUTER_BASE_URL"))
        self.assertTrue(profile.env_vars[-1].endswith("_BASE_URL"),
                        "Hermes reads the final *_BASE_URL entry as the user base-URL override")
        self.assertNotIn(profile.env_vars[-1], module.PROVIDER_NAME)

    def test_defaults_point_at_a_local_router(self):
        load_plugin({})
        profile = REGISTERED[0]
        self.assertEqual(profile.base_url, "http://localhost:20128/v1")
        self.assertEqual(profile.models_url, "http://localhost:20128/v1/models")
        self.assertIsNone(profile.default_aux_model, "no invented aux model id")
        self.assertEqual(profile.fallback_models, (), "the live catalogue is the list")

    def test_base_url_spellings_and_precedence(self):
        cases = [
            ({"HERMES_ROUTER_URL": "http://vps.example.com:20128"}, "http://vps.example.com:20128/v1"),
            ({"HERMES_ROUTER_URL": "http://vps.example.com:20128/"}, "http://vps.example.com:20128/v1"),
            ({"HERMES_ROUTER_URL": "https://router.example.com/v1"}, "https://router.example.com/v1"),
            ({"HERMES_ROUTER_URL": "vps.example.com"}, "http://vps.example.com/v1"),
            ({"HERMES_ROUTER_URL": "http://a.example.com", "HERMES_ROUTER_BASE_URL": "http://b.example.com"},
             "http://b.example.com/v1"),
        ]
        for env, expected in cases:
            load_plugin(env)
            self.assertEqual(REGISTERED[0].base_url, expected, env)

    def test_optional_env_knobs(self):
        load_plugin({"HERMES_ROUTER_AUX_MODEL": "mimo", "HERMES_ROUTER_MODELS": " mimo , sol ,mimo, "})
        profile = REGISTERED[0]
        self.assertEqual(profile.default_aux_model, "mimo")
        self.assertEqual(profile.fallback_models, ("mimo", "sol"))

    def test_register_is_idempotent_and_safe_without_a_context(self):
        module = load_plugin({})
        module.register()
        module.register(ctx=object())
        self.assertEqual(len(REGISTERED), 3)
        self.assertTrue(all(profile.name == "hermes-router" for profile in REGISTERED),
                        "re-registration must stay last-writer-wins with the SAME profile")
        self.assertEqual({p.base_url for p in REGISTERED}, {"http://localhost:20128/v1"})

    # ── live catalogue ─────────────────────────────────────────────────────
    def test_fetch_models_lists_the_router_catalogue(self):
        module = load_plugin({"HERMES_ROUTER_URL": self.base, "HERMES_ROUTER_KEY": "sk-test"})
        ids = REGISTERED[0].fetch_models()
        self.assertEqual(ids, ["mimo", "a6api/deepseek-v4.1-flash"])
        sent = STATE["requests"][-1]
        self.assertEqual(sent["path"], "/v1/models")
        self.assertEqual(sent["auth"], "Bearer sk-test")

    def test_fetch_models_accepts_other_wrappers(self):
        module = load_plugin({"HERMES_ROUTER_URL": self.base})
        for payload, expected in (
            ({"models": ["a", "b"]}, ["a", "b"]),
            ({"results": [{"name": "c"}]}, ["c"]),
            (["d", {"id": "e"}, {"id": "e"}], ["d", "e"]),
        ):
            STATE["payload"] = payload
            self.assertEqual(REGISTERED[0].fetch_models(), expected, payload)

    def test_fetch_models_returns_none_so_the_picker_falls_back(self):
        load_plugin({"HERMES_ROUTER_URL": self.base})
        STATE["status"] = 401
        self.assertIsNone(REGISTERED[0].fetch_models())
        STATE["status"] = 200
        STATE["raw"] = "<html>gateway</html>"
        self.assertIsNone(REGISTERED[0].fetch_models())
        STATE["raw"] = None
        STATE["payload"] = {"object": "list", "data": []}
        self.assertIsNone(REGISTERED[0].fetch_models())

    def test_fetch_models_handles_an_unreachable_router(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        load_plugin({"HERMES_ROUTER_URL": f"http://127.0.0.1:{dead_port}"})
        self.assertIsNone(REGISTERED[0].fetch_models())

    def test_fetch_models_honours_an_explicit_base_url_argument(self):
        load_plugin({})
        self.assertEqual(REGISTERED[0].fetch_models(base_url=self.base), ["mimo", "a6api/deepseek-v4.1-flash"])
        self.assertEqual(STATE["requests"][-1]["path"], "/v1/models")
        self.assertEqual(REGISTERED[0].fetch_models(base_url=self.base + "/"), ["mimo", "a6api/deepseek-v4.1-flash"])

    def test_parse_catalog_ignores_junk(self):
        module = load_plugin({})
        self.assertEqual(module.parse_catalog({"data": [{"id": " ok "}, {"id": ""}, 7, None, {"name": "two"}]}), ["ok", "two"])
        self.assertEqual(module.parse_catalog("not a list"), [])
        self.assertEqual(module.parse_catalog(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
