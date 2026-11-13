"""Configuration for the Hermes Router web-provider plugin.

Everything is environment driven on purpose:

* ``is_available()`` must answer without touching the network, and
* a plugin must not invent a second config file that the operator then has to
  keep in sync with the router's own ``.env``.

    HERMES_ROUTER_URL             base URL of the router (required)
    HERMES_ROUTER_BASE_URL        same value, under the name Hermes uses for a
                                  user base-URL override (the sibling model-provider
                                  plugin declares that slot, so both spellings work)
    HERMES_ROUTER_KEY             API key, only when the router requires auth
    HERMES_ROUTER_SEARCH_MODEL    model/provider for POST /v1/search   (default auto)
    HERMES_ROUTER_FETCH_MODEL     model/provider for POST /v1/web/fetch (default auto)
    HERMES_ROUTER_SEARCH_TIMEOUT_S  seconds, default 30
    HERMES_ROUTER_FETCH_TIMEOUT_S   seconds, default 110
    HERMES_ROUTER_MAX_CHARACTERS    hard cap for extracted text (0 = router default)

``auto`` is a real router value, not a stand-in: with it the router's own smart
router picks a provider (and fails over to the next one) so this plugin never
has to know which search/extract providers the operator bought.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_SEARCH_MODEL = "auto"
DEFAULT_FETCH_MODEL = "auto"
DEFAULT_SEARCH_TIMEOUT_S = 30.0
# Hermes' own web.extract_timeout defaults to 120s. Staying just under it means
# a slow page fails HERE, with a per-URL error document, instead of the whole
# tool call being cut off by the wrapper.
DEFAULT_FETCH_TIMEOUT_S = 110.0


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _env_int(name: str) -> Optional[int]:
    raw = _env(name)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def normalize_base_url(raw: Optional[str]) -> Optional[str]:
    """Return ``<base>/v1`` for whatever spelling the operator used.

    ``http://host:20128``, ``http://host:20128/`` and ``http://host:20128/v1``
    all mean the same API root; accepting one spelling and rejecting the others
    is how an integration becomes "the plugin is broken" in a support thread.
    """
    if not raw:
        return None
    base = raw.strip().rstrip("/")
    if not base:
        return None
    if not base.startswith("http://") and not base.startswith("https://"):
        base = "http://" + base
    if base.endswith("/v1"):
        return base
    return base + "/v1"


@dataclass(frozen=True)
class RouterConfig:
    base_url: Optional[str]
    api_key: Optional[str]
    search_model: str
    fetch_model: str
    search_timeout_s: float
    fetch_timeout_s: float
    max_characters: Optional[int]

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def endpoints(self) -> dict:
        base = self.base_url or ""
        return {"search": base + "/search", "fetch": base + "/web/fetch"}


def load_config() -> RouterConfig:
    return RouterConfig(
        base_url=normalize_base_url(_env("HERMES_ROUTER_URL") or _env("HERMES_ROUTER_BASE_URL")),
        api_key=_env("HERMES_ROUTER_KEY"),
        search_model=_env("HERMES_ROUTER_SEARCH_MODEL") or DEFAULT_SEARCH_MODEL,
        fetch_model=_env("HERMES_ROUTER_FETCH_MODEL") or DEFAULT_FETCH_MODEL,
        search_timeout_s=_env_float("HERMES_ROUTER_SEARCH_TIMEOUT_S", DEFAULT_SEARCH_TIMEOUT_S),
        fetch_timeout_s=_env_float("HERMES_ROUTER_FETCH_TIMEOUT_S", DEFAULT_FETCH_TIMEOUT_S),
        max_characters=_env_int("HERMES_ROUTER_MAX_CHARACTERS"),
    )
