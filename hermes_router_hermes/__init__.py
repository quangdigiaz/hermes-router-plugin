"""Hermes Router plugin — ONE install for inference, web_search and web_extract.

The web half is declared exactly like the first-party ``tinyfish`` plugin: a
``kind: backend`` manifest advertising ``provides_web_providers``, one
``register(ctx)`` that puts a provider instance into Hermes' web-search registry,
and optional CLI / in-session surfaces for configuration and diagnosis.

The model half (``.model_profile``) declares a ``ProviderProfile`` and registers
it with Hermes' provider registry — the shape the bundled providers and
`clinepass`-style plugins use. It used to be a SECOND plugin with its own
install command; it now rides this one, because the two halves answer to the
same URL and the same key. Why this arrangement:

* a manifest declares ONE ``kind`` (``kind: backend`` here — the documented path
  for a web backend, and the path whose loader IMPORTS the package), so the
  inference surface cannot be declared in a second manifest of the same install;
* the import is therefore the wiring: ``hermes_router_hermes.model_profile``
  registers its profile at module level the moment this package is imported,
  which is exactly how Hermes discovers out-of-tree providers.
* ``model_profile.py`` imports nothing from this package, so it ALSO works as the
  documented drop-in ``$HERMES_HOME/plugins/model-providers/hermes-router/__init__.py``
  (read on the next provider lookup, no restart). That is the escape hatch if a
  given Hermes build loads plugins after its first provider lookup.

    hermes plugins install quangdigiaz/hermes-router-plugin --enable

Nothing here is required for either provider: if a Hermes build does not expose
the CLI or command registration API, both providers still register and their
tools still work. That is deliberate — an integration whose value is the
router's provider pool must not fail to load because an optional surface name
changed.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional

from . import model_profile
from .config import load_config, normalize_base_url
from .provider import PROVIDER_NAME, STANDALONE, HermesRouterWebProvider

__version__ = "0.2.1"

__all__ = ["register", "HermesRouterWebProvider", "PROVIDER_NAME", "__version__"]

CLI_NAME = "hermes-router"
SLASH_NAME = "hermes-router-status"


def _log(kind: str, message: str) -> None:  # pragma: no cover - display only
    prefix = {"info": "[hermes-router]", "warn": "[hermes-router][warn]", "error": "[hermes-router][error]"}[kind]
    print(f"{prefix} {message}")


# ── Model half: status + checks ─────────────────────────────────────────────
def model_checks(live: bool = False, inside_hermes: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Checks for the inference surface, shaped like the web provider's.

    Offline covers everything that can be known without the router; ``live`` adds
    the one call that proves the ids the picker would show are readable.

    ``inside_hermes`` separates the two readings of "no provider registry here":
    running outside Hermes (this module imported by a CLI or a test — nothing to
    report) versus loaded by Hermes itself while its provider registry is not
    importable (a real wiring failure). Only the second may fail the check.
    """
    if inside_hermes is None:
        # The web ABC importing from `agent.web_search_provider` is the same
        # evidence, already measured by provider.py at import time.
        inside_hermes = not STANDALONE
    status = model_profile.model_status()
    registered = status["registry_importable"]
    checks: List[Dict[str, Any]] = [
        {
            "name": "model",
            "ok": bool(status["base_url"]) and (registered or not inside_hermes),
            "detail": (
                f"{status['name']} → {status['base_url']}"
                + ("" if registered else " (no provider registry in this process)")
            ),
        }
    ]
    if not live:
        ids, source = model_profile.catalog_ids(live=False)
        checks.append(
            {
                "name": "catalog",
                "ok": True,
                "detail": (
                    f"{len(ids)} curated ids ({source}): {', '.join(ids[:4])}"
                    if ids
                    else "no HERMES_ROUTER_MODELS — Hermes reads the live catalogue from the router"
                ),
            }
        )
        return checks

    ids, source = model_profile.catalog_ids(live=True)
    combos = [i for i in ids if "/" not in i]
    checks.append(
        {
            "name": "catalog",
            "ok": bool(ids),
            "detail": (
                f"{len(ids)} ids live — {len(ids) - len(combos)} provider/model, {len(combos)} combos: "
                f"{', '.join(ids[:4])}"
                if ids
                else f"GET /v1/models returned nothing ({source}) — check HERMES_ROUTER_URL / HERMES_ROUTER_KEY"
            ),
        }
    )
    return checks


def _print_model_status() -> int:
    status = model_profile.model_status()
    _log("info", f"inference    {status['name']}  (aliases: {', '.join(status['aliases']) or '—'})")
    _log("info", f"base url     {status['base_url']}")
    _log("info", f"catalogue    {status['models_url']}")
    _log("info", f"auth         {status['auth']}   api mode {status['api_mode']}   vision {status['supports_vision']}")
    if not status["registry_importable"]:
        _log("warn", "loaded outside Hermes — no provider registry to register into")
    _log("info", "use it       hermes chat -q \"…\" --provider " + status["name"] + " -m <id>")
    return 0


# ── CLI + slash surfaces ────────────────────────────────────────────────────
def _cli_setup(subparser: argparse.ArgumentParser) -> None:
    """argparse tree for ``hermes hermes-router`` (required by the current
    PluginContext.register_cli_command signature — without it the CLI surface
    is skipped and `hermes hermes-router …` does not exist). REMAINDER keeps
    flags like --live/--format as raw strings so _dispatch_cli's own subcommand
    parsing stays authoritative."""
    subparser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="status | setup | doctor [--live] | model [--live] | search <query> | fetch <url> [--format markdown]",
    )


def _argv(args: Any) -> List[str]:
    if args is None:
        return []
    if isinstance(args, str):
        return args.split()
    remainder = getattr(args, "args", None)  # argparse.Namespace from setup_fn
    if remainder is not None:
        return [str(item) for item in remainder]
    try:
        return [str(item) for item in args]
    except TypeError:
        return [str(args)]


def _print_status(provider: HermesRouterWebProvider) -> None:
    status = provider.status()
    _log("info", f"backend      {status['provider']}")
    _log("info", f"base url     {status['base_url'] or '(unset: export HERMES_ROUTER_URL)'}")
    _log("info", f"auth         {status['auth']}")
    _log("info", f"search model {status['search_model']}   fetch model {status['fetch_model']}")
    _log("info", f"timeouts     search {status['search_timeout_s']}s   fetch {status['fetch_timeout_s']}s")
    model = model_profile.model_status()
    _log("info", f"inference    {model['name']} @ {model['catalogue_url']} (auth {model['auth']})")
    _log("info", "wire it up   hermes config set web.search_backend " + PROVIDER_NAME)
    _log("info", "             hermes config set web.extract_backend " + PROVIDER_NAME)
    _log("info", "             hermes config set model.provider " + model["name"])


def _print_doctor(report: dict) -> None:
    for check in report["checks"]:
        mark = "ok  " if check.get("ok") else "FAIL"
        _log("info", f"{mark} {check['name']:<10} {check.get('detail')}")
    if report.get("hint"):
        _log("info", report["hint"])
    if not report.get("ok"):
        _log("error", "doctor found a failing check")


def _merged_doctor(provider: HermesRouterWebProvider, live: bool = False) -> Dict[str, Any]:
    """Web checks (offline config, live search+fetch) + the inference checks.

    One doctor for the one install: an operator diagnosing "the agent is not
    answering" and "the agent cannot search" should not have to run two.
    """
    report = provider.doctor(live=live)
    # A missing HERMES_ROUTER_URL fails the web config check and returns early;
    # the model half keys off the same variable, so its checks are appended
    # either way — the reader sees ONE root cause instead of two symptoms.
    report["checks"].extend(model_checks(live=live and provider.is_available(), inside_hermes=not STANDALONE))
    report.setdefault("status", {})["model"] = model_profile.model_status()
    report["ok"] = all(check["ok"] for check in report["checks"])
    if not live and report.get("hint"):
        report["hint"] = "pass --live to run one real search, one real fetch and one real catalogue fetch"
    return report


def _dispatch_cli(args: Any) -> int:
    """Handler for ``hermes hermes-router <subcommand>``."""
    provider = HermesRouterWebProvider()
    argv = _argv(args)
    command = argv[0] if argv else "status"

    if command in ("status", ""):
        _print_status(provider)
        return 0 if provider.is_available() else 1

    if command == "setup":
        # Deliberately advisory: writing Hermes' own config is the host's job,
        # and a plugin that silently rewrites `web.search_backend` is a plugin
        # that cannot be reasoned about from the config file afterwards.
        url = next((a for a in argv[1:] if a.startswith("http")), None)
        if url:
            _log("info", f"export HERMES_ROUTER_URL={url}")
        _log("info", "one URL + one key serve all three surfaces: inference, web_search, web_extract")
        _log("info", "then, only if you want the router pinned (Hermes auto-detects otherwise):")
        _log("info", "  hermes config set web.search_backend " + PROVIDER_NAME)
        _log("info", "  hermes config set web.extract_backend " + PROVIDER_NAME)
        _log("info", "  hermes config set model.provider " + model_profile.PROVIDER_NAME)
        return 0

    if command == "doctor":
        report = _merged_doctor(provider, live="--live" in argv)
        _print_doctor(report)
        return 0 if report.get("ok") else 1

    if command == "model":
        code = _print_model_status()
        ids, source = model_profile.catalog_ids(live="--live" in argv)
        _log("info", f"catalogue    {len(ids)} ids ({source})")
        for model_id in ids[:10]:
            kind = "combo" if "/" not in model_id else "model"
            _log("info", f"  {model_id}  [{kind}]")
        return code

    if command == "search":
        query = " ".join(a for a in argv[1:] if not a.startswith("--"))
        if not query:
            _log("error", "usage: hermes hermes-router search \"<query>\"")
            return 2
        result = provider.search(query, limit=5)
        if not result.get("success"):
            _log("error", result.get("error", "search failed"))
            return 1
        for item in result["data"]["web"]:
            _log("info", f"{item['position']}. {item['title']}\n    {item['url']}\n    {item['description'][:160]}")
        return 0

    if command == "fetch":
        target = next((a for a in argv[1:] if not a.startswith("--")), None)
        if not target:
            _log("error", "usage: hermes hermes-router fetch <url> [--format markdown]")
            return 2
        fmt = "markdown"
        if "--format" in argv:
            index = argv.index("--format")
            if index + 1 < len(argv):
                fmt = argv[index + 1]
        result = provider.extract([target], format=fmt)
        document = (result.get("data") or [{}])[0]
        if document.get("error"):
            _log("error", f"{document['url']}: {document['error']}")
            return 1
        _log("info", f"{document.get('title') or document['url']} — {len(document.get('content') or '')} chars")
        return 0

    _log("error", f"unknown subcommand '{command}' (status | setup | doctor [--live] | model [--live] | search | fetch)")
    return 2


def _status_slash(raw_args: Any) -> str:
    provider = HermesRouterWebProvider()
    live = "live" in str(raw_args or "").lower()
    if live:
        report = _merged_doctor(provider, live=True)
        lines = [f"hermes-router: {'ok' if report.get('ok') else 'FAILING'}"]
        lines += [f"  {'ok' if c.get('ok') else 'FAIL'} {c['name']}: {c.get('detail')}" for c in report["checks"]]
        return "\n".join(lines)
    status = provider.status()
    model = model_profile.model_status()
    return (
        "hermes-router (inference + web search + fetch)\n"
        f"  base url: {status['base_url'] or '(unset — export HERMES_ROUTER_URL)'}\n"
        f"  web:      search {status['search_model']}   fetch {status['fetch_model']}   auth {status['auth']}\n"
        f"  model:    {model['name']} @ {model['catalogue_url']}   auth {model['auth']}\n"
        "  pass 'live' to run one real search, one real fetch and one catalogue fetch"
    )


def _register_optional_surfaces(ctx: Any) -> None:
    register_cli = getattr(ctx, "register_cli_command", None)
    register_command = getattr(ctx, "register_command", None)
    if callable(register_cli):
        try:
            register_cli(
                name=CLI_NAME,
                help="Configure and diagnose the Hermes Router (inference + web search + fetch)",
                setup_fn=_cli_setup,
                handler_fn=_dispatch_cli,
                description="Route Hermes inference, web_search and web_extract through a self-hosted Hermes Router.",
            )
        except Exception as exc:  # pragma: no cover - host API variance
            _log("warn", f"CLI surface not registered: {exc}")
    if callable(register_command):
        try:
            register_command(
                name=SLASH_NAME,
                handler=_status_slash,
                description="Show the Hermes Router status for all three surfaces; add 'live' to test them.",
                args_hint="[live]",
            )
        except Exception as exc:  # pragma: no cover - host API variance
            _log("warn", f"slash command not registered: {exc}")


def register(ctx: Any) -> None:
    """Plugin entry point — called once at load time.

    One call wires all three surfaces:
      * ``web_search`` / ``web_extract`` → this provider (registry above),
      * inference → the model profile (registered at import of
        ``.model_profile``, and again here so a loader that only calls
        ``register(ctx)`` is covered too — last-writer-wins with the same
        profile makes that harmless).
    """
    provider = HermesRouterWebProvider()
    ctx.register_web_search_provider(provider)
    _register_optional_surfaces(ctx)

    # The model half. Re-importing is how a loader that skipped the module-level
    # side effect still ends up with the profile in the registry.
    model_profile.register(ctx)
    if not model_profile.model_registration_available():
        _log("warn", "no Hermes provider registry — inference surface not registered in this process")

    if STANDALONE:
        # Importable without Hermes is what makes this testable; loading it
        # INSIDE Hermes with the stand-in would mean the real ABC moved.
        _log("warn", "loaded without Hermes' web_search_provider ABC — provider registered on a stand-in base class")
    elif not provider.is_available():
        _log("warn", "registered, but HERMES_ROUTER_URL is unset so inference, web_search and web_extract will skip it")

    register_config = getattr(ctx, "register_config", None)
    if callable(register_config):  # pragma: no cover - optional host surface
        try:
            config = load_config()
            register_config(
                {
                    "hermes_router": {
                        "base_url_env": "HERMES_ROUTER_URL",
                        "api_key_env": "HERMES_ROUTER_KEY",
                        "search_model": config.search_model,
                        "fetch_model": config.fetch_model,
                        "model_provider": model_profile.PROVIDER_NAME,
                    }
                }
            )
        except Exception as exc:  # pragma: no cover - host API variance
            _log("warn", f"config surface not registered: {exc}")


def resolve_base_url(raw: Optional[str]) -> Optional[str]:
    """Exposed for the CLI and tests: one spelling rule for the base URL."""
    return normalize_base_url(raw)
