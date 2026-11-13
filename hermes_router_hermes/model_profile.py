"""Hermes Router as a Hermes Agent **model provider** — part of the merged plugin.

Declared the way the bundled providers and community plugins such as
`clinepass` are declared: one `ProviderProfile` registered at module level, and
Hermes wires everything else from it — the `--provider` flag and aliases, the
`/model` picker, `hermes doctor`, `hermes setup`, URL reverse-mapping, the
auxiliary client and runtime resolution. No edits to the Hermes repo.

ONE install wires all three surfaces: the merged ``hermes-plugin`` package
imports this module (``hermes_router_hermes/__init__.py``), so whichever loader
path Hermes takes for a ``kind: backend`` plugin — the general PluginManager,
which imports the package and calls ``register(ctx)`` — the profile is
registered on the way in, against the SAME ``HERMES_ROUTER_URL`` /
``HERMES_ROUTER_KEY`` pair the web provider uses.

It is also the documented drop-in for Hermes' own model-provider discovery path
(``$HERMES_HOME/plugins/model-providers/<name>/``, read on the next provider
lookup — no restart):

    cp hermes_router_hermes/model_profile.py \\
       "$HERMES_HOME/plugins/model-providers/hermes-router/__init__.py"

Which is why this file imports NOTHING from its own package — only stdlib and
``providers``. A self-contained file can be copied anywhere; a package-relative
import could not. ``tests/test_merged_plugin.py`` pins that contract.

What the profile buys an operator: their agent talks to the router instead of a
single vendor. Every model id Hermes sees (`<provider>/<model>`) and every combo
(bare name) comes from the router's own pool, so model choice, fail-over,
cost/health ranking and the usage ledger stay in one place.

    HERMES_ROUTER_URL / HERMES_ROUTER_BASE_URL   base URL (default: localhost:20128)
    HERMES_ROUTER_KEY                            API key, only when the router requires one
    HERMES_ROUTER_AUX_MODEL                      optional cheap model for aux tasks
    HERMES_ROUTER_MODELS                         optional comma-separated fallback ids

Deliberate omissions, because inventing values would be worse than omitting them:

* ``default_aux_model`` is only set when the operator names one. A router's
  model ids are per-install (a combo called ``mimo`` exists on one box and not
  on the next), so a hardcoded id would 404 for everyone else.
* ``model_capabilities`` is not declared for the same reason — the pool is not
  knowable from the plugin. Per-model vision/reasoning truth comes from the
  router's own catalogue; declare exceptions under
  ``model_overrides.hermes-router.<id>`` in config.yaml (user config wins over
  any plugin declaration).
* ``fallback_models`` stays empty unless ``HERMES_ROUTER_MODELS`` says otherwise:
  the live catalogue (`GET /v1/models`) is the real list.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

PROVIDER_NAME = "hermes-router"
DEFAULT_BASE_URL = "http://localhost:20128/v1"
CATALOG_TIMEOUT_S = 8.0

try:  # inside Hermes
    from providers import register_provider as _register_provider
    from providers.base import ProviderProfile as _ProviderProfile

    STANDALONE = False
except Exception:  # pragma: no cover - exercised only outside Hermes
    _register_provider = None

    @dataclass
    class _ProviderProfile:  # type: ignore[no-redef]
        """Stand-in mirroring the documented ProviderProfile fields.

        It accepts ONLY the fields this plugin declares, so a field name that
        Hermes does not have fails loudly at import instead of being ignored —
        which is exactly the class of bug a "declaration" plugin can ship with.
        """

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
        fallback_models: Tuple[str, ...] = field(default_factory=tuple)
        supports_vision: bool = False

        def fetch_models(self, *, api_key=None, base_url=None, timeout=CATALOG_TIMEOUT_S):
            return None

    STANDALONE = True


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def normalize_base_url(raw: Optional[str]) -> str:
    """One spelling rule for the router root: `<base>/v1`, defaulting to local.

    `HERMES_ROUTER_URL` is the convention this project's skills and web plugin
    already publish; `HERMES_ROUTER_BASE_URL` is the slot Hermes uses for a
    user base-URL override. Both are accepted, and neither may end up as
    `/v1/v1`.
    """
    base = (raw or "").strip()
    if not base:
        return DEFAULT_BASE_URL
    base = base.rstrip("/")
    if not base.startswith("http://") and not base.startswith("https://"):
        base = "http://" + base
    return base if base.endswith("/v1") else base + "/v1"


def resolve_base_url() -> str:
    return normalize_base_url(_env("HERMES_ROUTER_BASE_URL") or _env("HERMES_ROUTER_URL"))


def resolve_fallback_models() -> Tuple[str, ...]:
    raw = _env("HERMES_ROUTER_MODELS")
    if not raw:
        return ()
    seen: List[str] = []
    for item in raw.split(","):
        model = item.strip()
        if model and model not in seen:
            seen.append(model)
    return tuple(seen)


def _catalog_url(base_url: str, models_url: Optional[str] = None) -> str:
    """`{base}/models` for either spelling of the base URL."""
    if models_url:
        return models_url
    base = base_url.rstrip("/")
    return base + "/models" if base.endswith("/v1") else base + "/v1/models"


def parse_catalog(payload: Any) -> List[str]:
    """Model ids out of the router's OpenAI-style list.

    The router answers `{"object":"list","data":[{"id": ...}]}`; `models` and
    `results` are accepted because a self-hosted gateway in front of the router
    may re-wrap it, and a wrapped catalogue must not look like "no models".
    """
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data") or payload.get("models") or payload.get("results") or []
    else:
        return []
    if not isinstance(items, list):
        return []
    ids: List[str] = []
    for item in items:
        if isinstance(item, str):
            model_id = item.strip()
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
        else:
            continue
        if model_id and model_id not in ids:
            ids.append(model_id)
    return ids


class HermesRouterProfile(_ProviderProfile):
    """The profile Hermes registers; only the catalogue fetch is overridden."""

    def fetch_models(self, *, api_key: Optional[str] = None, base_url: Optional[str] = None, timeout: float = CATALOG_TIMEOUT_S) -> Optional[List[str]]:
        """Live catalogue. Any failure returns None so Hermes uses the curated
        `fallback_models` instead of showing an empty picker."""
        url = _catalog_url(base_url or self.base_url or DEFAULT_BASE_URL, self.models_url if not base_url else None)
        headers = {"accept": "application/json"}
        key = api_key or _env("HERMES_ROUTER_KEY")
        if key:
            headers["authorization"] = "Bearer " + key
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace") or "null")
        except urllib.error.HTTPError as exc:
            exc.close()  # an unread error body would otherwise be an open handle
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return None
        ids = parse_catalog(payload)
        return ids or None


def build_profile() -> HermesRouterProfile:
    base_url = resolve_base_url()
    return HermesRouterProfile(
        name=PROVIDER_NAME,
        aliases=("hermesrouter", "hermes_router"),
        display_name="Hermes Router",
        description="Self-hosted multi-provider gateway — the router's own model and combo pool, with fail-over",
        signup_url="https://github.com/quangdigiaz/hermes-router",
        # Final *_BASE_URL entry is Hermes' user base-URL override slot; the
        # profile also honours HERMES_ROUTER_URL (this project's convention).
        env_vars=("HERMES_ROUTER_KEY", "HERMES_ROUTER_BASE_URL"),
        base_url=base_url,
        models_url=base_url + "/models",
        auth_type="api_key",
        api_mode="chat_completions",
        default_aux_model=_env("HERMES_ROUTER_AUX_MODEL"),
        fallback_models=resolve_fallback_models(),
        # The router proxies image content straight through to vision models, so
        # images inside tool results must not be replaced by a text description.
        supports_vision=True,
    )


PROFILE = build_profile()

if _register_provider is not None:
    _register_provider(PROFILE)


def model_registration_available() -> bool:
    """Is Hermes' provider registry importable in this process?

    False means the module is running outside Hermes (CLI, tests, the drop-in
    copied next to a different Hermes install) — reported by ``doctor`` instead
    of being guessed at, because "the profile was not registered" and "there is
    no registry to register it with" are different support conversations.
    """
    return _register_provider is not None


def model_status() -> dict:
    """Everything the CLI/doctor prints about the inference surface. Offline."""
    return {
        "name": PROFILE.name,
        "aliases": list(PROFILE.aliases or ()),
        "base_url": PROFILE.base_url,
        "models_url": PROFILE.models_url,
        "catalogue_url": _catalog_url(PROFILE.base_url or DEFAULT_BASE_URL, None),
        "auth": "api_key" if _env("HERMES_ROUTER_KEY") else "none",
        "api_mode": PROFILE.api_mode,
        "supports_vision": bool(PROFILE.supports_vision),
        "default_aux_model": PROFILE.default_aux_model,
        "fallback_models": list(PROFILE.fallback_models or ()),
        "registry_importable": model_registration_available(),
    }


def catalog_ids(live: bool = False, timeout: float = CATALOG_TIMEOUT_S) -> tuple:
    """The ids the ``/model`` picker would show.

    Offline it is the curated ``HERMES_ROUTER_MODELS`` list; ``live=True`` asks
    the router (``GET /v1/models``) the way Hermes does. Returns
    ``(ids, source)`` where source is ``live`` / ``fallback`` / ``unset``, so a
    caller can say WHERE a picker's contents came from instead of implying the
    router was reachable.
    """
    if live:
        fetched = PROFILE.fetch_models(timeout=timeout)
        if fetched:
            return list(fetched), "live"
        return list(PROFILE.fallback_models or ()), "fallback"
    curated = list(PROFILE.fallback_models or ())
    return curated, ("fallback" if curated else "unset")


def register(ctx: Any = None) -> None:
    """Loader entry point (``register(ctx)`` and the no-arg form).

    A model provider is declared at module level (the documented contract for
    ``plugins/model-providers/<name>/__init__.py``). This function exists so a
    loader that expects ``register(ctx)`` — the shape every other plugin kind
    uses, and the shape the merged package's own entry point calls — still gets
    the same profile registered, and it is idempotent because
    `register_provider` is last-writer-wins with an identical profile.
    """
    # Silent on purpose: the merged plugin's entry point reports the missing
    # registry once, and the drop-in copy only ever runs INSIDE Hermes (where
    # the registry is importable). A second warning from here was noise on every
    # `register(ctx)` in the tests and the CLI.
    if _register_provider is None:
        return
    _register_provider(PROFILE)
