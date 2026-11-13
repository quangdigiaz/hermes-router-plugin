"""The merge's own contract: ONE install wires all three surfaces.

Run: ``python tests/test_merged_plugin.py`` (also collects under pytest).

What the merge promises an operator, pinned here:

1. one ``register(ctx)`` registers BOTH a web provider (search + extract) and an
   inference profile, from the same import — no second plugin, no second install;
2. the profile is registered by the IMPORT alone (that is the discovery interface
   Hermes documents: ``register_provider`` at module level), so a loader that
   only imports the package still gets inference;
3. ONE env pair (``HERMES_ROUTER_URL`` / ``HERMES_ROUTER_KEY``) serves all three
   surfaces, and the manifest asks for exactly that pair;
4. the web provider id, the model provider id and the manifest's advertised
   ``provides_web_providers`` name are the same string — one router, one name;
5. ``model_profile.py`` stays self-contained (no package-relative imports), which
   is what makes the documented drop-in path work:
   ``cp hermes_router_hermes/model_profile.py $HERMES_HOME/plugins/model-providers/hermes-router/__init__.py``.

``providers`` / ``providers.base`` are stubbed with ONLY the documented
ProviderProfile fields (a field Hermes does not have must fail the import, not be
silently dropped). Hermes itself is not required.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Optional, Tuple

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = PLUGIN_DIR / "hermes_router_hermes"
MODEL_PROFILE_FILE = PACKAGE_DIR / "model_profile.py"
MANIFEST_FILE = PLUGIN_DIR / "plugin.yaml"
sys.path.insert(0, str(PLUGIN_DIR))

REGISTERED: list = []


def _install_stub_providers() -> None:
    """Stub ``providers`` with the documented profile fields (see test_model_profile)."""

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


class FakeCtx:
    """The PluginContext surface this plugin uses, recording what it received."""

    def __init__(self) -> None:
        self.providers: list = []
        self.cli: dict = {}
        self.slash: dict = {}
        self.config: dict = {}
        self.warnings: list = []

    def register_web_search_provider(self, provider: Any) -> None:
        self.providers.append(provider)

    def register_cli_command(self, **kwargs) -> None:
        self.cli = kwargs

    def register_command(self, **kwargs) -> None:
        self.slash = kwargs

    def register_config(self, payload: dict) -> None:
        self.config = payload


def load_package(env: dict) -> Any:
    """Import the plugin package FRESH, so module-level registration re-runs."""
    for key in ("HERMES_ROUTER_URL", "HERMES_ROUTER_BASE_URL", "HERMES_ROUTER_KEY"):
        os.environ.pop(key, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    REGISTERED.clear()
    _install_stub_providers()
    sys.modules.pop("hermes_router_hermes", None)
    sys.modules.pop("hermes_router_hermes.provider", None)
    sys.modules.pop("hermes_router_hermes.config", None)
    sys.modules.pop("hermes_router_hermes.model_profile", None)
    sys.modules.pop("hermes_router_hermes.client", None)
    import hermes_router_hermes  # noqa: PLC0415 - deliberately after the stubs

    return hermes_router_hermes


def read_manifest() -> dict:
    """Parse the handful of manifest fields this test asserts on.

    Hand-rolled rather than pyyaml: the manifest check must run on a bare
    interpreter too (the plugin has no runtime dependencies, and neither do its
    tests) — and a plugin whose manifest cannot be read is exactly what this
    catches.
    """
    text = MANIFEST_FILE.read_text(encoding="utf-8")
    out: dict = {"provides_web_providers": [], "requires_env": []}
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if not line.startswith((" ", "\t", "-")) and ":" in line:
            key, _, value = line.partition(":")
            section = key.strip()
            value = value.strip().strip('"')
            if section in ("provides_web_providers", "requires_env"):
                out[section] = []
            elif value:
                out[section] = value
            continue
        item = line.strip()
        if section == "provides_web_providers" and item.startswith("- "):
            out["provides_web_providers"].append(item[2:].strip())
        elif section == "requires_env" and item.startswith("- name:"):
            out["requires_env"].append(item.split(":", 1)[1].strip().strip('"'))
    return out


class MergedPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._env = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    # ── one install, three surfaces ─────────────────────────────────────────
    def test_register_wires_web_search_extract_and_inference_together(self):
        module = load_package({"HERMES_ROUTER_URL": "http://localhost:20128"})
        ctx = FakeCtx()
        module.register(ctx)

        self.assertEqual(len(ctx.providers), 1, "exactly one web provider")
        web = ctx.providers[0]
        self.assertEqual(web.name, module.PROVIDER_NAME)
        self.assertTrue(web.supports_search())
        self.assertTrue(web.supports_extract())

        # Inference is registered twice on purpose — once by the import (module
        # level, Hermes' documented discovery interface) and once by register(ctx)
        # for a loader that only calls that. `register_provider` is
        # last-writer-wins, so both entries must be the SAME profile; a differing
        # second one would be the bug this pins.
        self.assertGreaterEqual(len(REGISTERED), 1, "inference must be registered by the same call")
        self.assertEqual({profile.name for profile in REGISTERED}, {module.PROVIDER_NAME},
                         "one router, one name: the web id, the model id and the manifest agree")
        self.assertEqual({profile.base_url for profile in REGISTERED}, {REGISTERED[-1].base_url},
                         "re-registration must be idempotent")

    def test_the_import_alone_registers_inference(self):
        # Hermes' documented discovery for an out-of-tree provider is the
        # module-level register_provider() — a loader that only imports the
        # package (no register(ctx)) must still get inference.
        module = load_package({})
        self.assertEqual(len(REGISTERED), 1)
        self.assertEqual(REGISTERED[0].name, module.model_profile.PROVIDER_NAME)

    def test_one_env_pair_serves_all_three_surfaces(self):
        module = load_package({"HERMES_ROUTER_URL": "https://router.example.com", "HERMES_ROUTER_KEY": "sk-abc"})
        ctx = FakeCtx()
        module.register(ctx)

        web_status = ctx.providers[0].status()
        profile = REGISTERED[0]
        # Same host, same /v1 root, same key: nothing here needs a second export,
        # and a typo'd second URL cannot silently desync web from inference.
        self.assertEqual(web_status["base_url"], profile.base_url)
        self.assertEqual(profile.base_url, "https://router.example.com/v1")
        self.assertEqual(profile.models_url, "https://router.example.com/v1/models")
        self.assertEqual(web_status["auth"], "api key")
        self.assertEqual(profile.env_vars, ("HERMES_ROUTER_KEY", "HERMES_ROUTER_BASE_URL"))

    def test_register_also_declares_the_config_surface_for_both_halves(self):
        module = load_package({"HERMES_ROUTER_URL": "http://localhost:20128"})
        ctx = FakeCtx()
        module.register(ctx)
        self.assertIn("hermes_router", ctx.config)
        declared = ctx.config["hermes_router"]
        self.assertEqual(declared["base_url_env"], "HERMES_ROUTER_URL")
        self.assertEqual(declared["api_key_env"], "HERMES_ROUTER_KEY")
        self.assertEqual(declared["model_provider"], module.PROVIDER_NAME)
        self.assertTrue(declared["search_model"] and declared["fetch_model"])

    def test_cli_and_slash_surfaces_are_registered_once(self):
        module = load_package({})
        ctx = FakeCtx()
        module.register(ctx)
        self.assertEqual(ctx.cli["name"], "hermes-router")
        self.assertEqual(ctx.slash["name"], "hermes-router-status")

    # ── the manifest ────────────────────────────────────────────────────────
    def test_manifest_declares_the_web_provider_under_its_registered_name(self):
        module = load_package({})
        ctx = FakeCtx()
        module.register(ctx)
        manifest = read_manifest()

        self.assertEqual(manifest["kind"], "backend",
                         "backend is the documented kind for a web provider AND the loader that imports the package")
        self.assertEqual(manifest["name"], "hermes-router")
        self.assertIn(ctx.providers[0].name, manifest["provides_web_providers"])

    def test_manifest_requires_exactly_the_env_pair_both_halves_read(self):
        manifest = read_manifest()
        self.assertEqual(manifest["requires_env"], ["HERMES_ROUTER_URL", "HERMES_ROUTER_KEY"])
        module = load_package({"HERMES_ROUTER_URL": "http://localhost:20128"})
        ctx = FakeCtx()
        module.register(ctx)
        # The web provider's own config reads the same two names (config.py), so
        # the manifest cannot drift away from what the code actually needs.
        from hermes_router_hermes.config import load_config  # noqa: PLC0415

        config = load_config()
        self.assertEqual(config.base_url, "http://localhost:20128/v1")
        self.assertIsNone(config.api_key)

    # ── the drop-in path ────────────────────────────────────────────────────
    def test_model_profile_stays_self_contained_for_the_copied_drop_in(self):
        source = MODEL_PROFILE_FILE.read_text(encoding="utf-8")
        relative = [line.strip() for line in source.splitlines()
                    if re.match(r"^\s*from\s+\.", line) or re.match(r"^\s*import\s+\.", line)]
        self.assertEqual(relative, [],
                         "a package-relative import would break the documented copy into "
                         "$HERMES_HOME/plugins/model-providers/hermes-router/")

        # And prove it by loading that file alone, exactly as the drop-in does.
        REGISTERED.clear()
        _install_stub_providers()
        name = "hermes_router_dropin_under_test"
        spec = importlib.util.spec_from_file_location(name, MODEL_PROFILE_FILE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        self.assertEqual(len(REGISTERED), 1)
        self.assertEqual(REGISTERED[0].name, "hermes-router")
        self.assertTrue(callable(module.register))

    # ── one doctor / one status for all three ───────────────────────────────
    def test_doctor_covers_all_three_surfaces_and_one_status_row_each(self):
        env = {"HERMES_ROUTER_URL": "http://localhost:20128"}
        module = load_package(env)
        os.environ["HERMES_ROUTER_URL"] = "http://localhost:20128"
        ctx = FakeCtx()
        module.register(ctx)

        report = module._merged_doctor(ctx.providers[0], live=False)
        names = [check["name"] for check in report["checks"]]
        self.assertEqual(names, ["config", "model", "catalog"])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["status"]["model"]["name"], "hermes-router")

        status = ctx.providers[0].status()
        self.assertEqual(status["provider"], "hermes-router")
        self.assertTrue(status["configured"])

    def test_cli_setup_prints_every_wiring_key_in_one_place(self):
        module = load_package({"HERMES_ROUTER_URL": "http://localhost:20128"})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module._dispatch_cli(["setup", "http://localhost:20128"])
        output = buffer.getvalue()
        self.assertEqual(code, 0)
        # The one-command story, printed: install wires the three surfaces; these
        # are the only config keys a user might still want to pin.
        for needle in ("export HERMES_ROUTER_URL=http://localhost:20128",
                       "web.search_backend hermes-router",
                       "web.extract_backend hermes-router",
                       "model.provider hermes-router"):
            self.assertIn(needle, output)

    def test_cli_model_reports_the_catalogue_source(self):
        module = load_package({})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = module._dispatch_cli(["model"])
        self.assertEqual(code, 0)
        self.assertIn("catalogue", buffer.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
