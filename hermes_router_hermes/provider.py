"""Hermes Router as a Hermes Agent web provider (search + extract).

Backed by the router's own two web surfaces, so the agent's ``web_search`` /
``web_extract`` tools inherit everything the router already does — provider
pooling, combo fail-over, SSRF guarding, clean-markdown extraction and its
usage ledger — without the agent holding a single search/extract vendor key.

    POST {HERMES_ROUTER_URL}/v1/search      {model, query, max_results}
    POST {HERMES_ROUTER_URL}/v1/web/fetch   {model, url, format, max_characters}

Contract (agent/web_search_provider.WebSearchProvider): the two envelopes this
class returns are fixed, because the tool wrapper must not have to translate
between backends. ``search()`` → ``{"success": True, "data": {"web": [...]}}``,
``extract()`` → ``{"success": True, "data": [ {url, title, content, ...} ]}``,
and a failure the caller can explain → ``{"success": False, "error": "..."}``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import RouterClient, RouterError, error_text, unwrap
from .config import RouterConfig, load_config

try:  # inside Hermes the ABC is importable; outside it (CLI, tests) we stand in.
    from agent.web_search_provider import WebSearchProvider as _HermesWebSearchProvider  # type: ignore

    STANDALONE = False
except Exception:  # pragma: no cover - exercised only when run outside Hermes

    class _HermesWebSearchProvider:  # type: ignore[no-redef]
        """Stand-in for the Hermes ABC so the module stays importable on its own."""

        @property
        def name(self) -> str:  # pragma: no cover - overridden
            raise NotImplementedError

        @property
        def display_name(self) -> str:
            return self.name

        def is_available(self) -> bool:  # pragma: no cover - overridden
            raise NotImplementedError

        def supports_search(self) -> bool:
            return True

        def supports_extract(self) -> bool:
            return False

    STANDALONE = True

PROVIDER_NAME = "hermes-router"
#: The router caps a single search at 20 results on most providers; asking for
#: more than the tool can use only spends the operator's quota.
MAX_RESULTS = 20
#: Rough characters-per-token used only when the caller caps OUTPUT TOKENS and
#: the router expects characters. Documented as an approximation, never silent.
CHARS_PER_TOKEN = 4


def _positive_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class HermesRouterWebProvider(_HermesWebSearchProvider):
    """Search + extract through a self-hosted Hermes Router."""

    def __init__(self, config: Optional[RouterConfig] = None, client_factory=None) -> None:
        # Config is read lazily-ish: the constructor takes an explicit config so
        # tests and the CLI can pin one, and falls back to the environment.
        self._config_override = config
        self._client_factory = client_factory or RouterClient

    # ── identity ────────────────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def display_name(self) -> str:
        return "Hermes Router"

    @property
    def config(self) -> RouterConfig:
        return self._config_override or load_config()

    def is_available(self) -> bool:
        """Cheap and offline: the router URL is the credential, not a key.

        This runs on every `hermes tools` paint, so it must never make a
        network call — reachability is what `doctor --live` is for.
        """
        return self.config.configured

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def _client(self) -> RouterClient:
        config = self.config
        if not config.base_url:
            raise RouterError("HERMES_ROUTER_URL is not set")
        return self._client_factory(config.base_url, config.api_key)

    # ── search ──────────────────────────────────────────────────────────────
    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        if not query or not str(query).strip():
            return {"success": False, "error": "search called without a query"}
        config = self.config
        max_results = max(1, min(_positive_int(limit) or 5, MAX_RESULTS))
        body: Dict[str, Any] = {"model": config.search_model, "query": str(query).strip(), "max_results": max_results}
        try:
            payload = unwrap(self._client().post("/search", body, config.search_timeout_s))
        except RouterError as exc:
            return {"success": False, "error": f"Hermes Router search failed: {exc}"}

        items = payload.get("results")
        if not isinstance(items, list):
            return {
                "success": False,
                "error": error_text(payload, "Hermes Router search returned no results list"),
            }
        web: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            web.append(
                {
                    "title": str(item.get("title") or url),
                    "url": url,
                    # The router's snippet is the search engine's own text; its
                    # `content` (when a provider returns one) is the fuller page
                    # body, which is a better description than nothing.
                    "description": str(item.get("snippet") or item.get("content") or ""),
                    "position": _positive_int(item.get("position")) or index + 1,
                }
            )
        if not web:
            provider = payload.get("provider") or config.search_model
            return {"success": False, "error": f"Hermes Router search via {provider} returned 0 usable results"}
        return {"success": True, "data": {"web": web}}

    # ── extract ─────────────────────────────────────────────────────────────
    def extract(self, urls: List[str], **kwargs: Any) -> Dict[str, Any]:
        if not urls:
            return {"success": False, "error": "extract called without a url"}
        # An unconfigured backend is a configuration failure, not a per-URL one:
        # returning error documents for every URL would read as "the pages are
        # unreachable" and send the operator hunting the wrong problem.
        if not self.is_available():
            return {"success": False, "error": "HERMES_ROUTER_URL is not set"}
        config = self.config
        fmt = str(kwargs.get("format") or "markdown").lower()
        if fmt not in ("markdown", "text", "html"):
            fmt = "markdown"
        max_characters = _positive_int(kwargs.get("max_characters"))
        if max_characters is None and kwargs.get("max_tokens") is not None:
            tokens = _positive_int(kwargs.get("max_tokens"))
            if tokens:
                max_characters = tokens * CHARS_PER_TOKEN
        if max_characters is None:
            max_characters = config.max_characters

        documents = [self._extract_one(str(url), fmt, max_characters) for url in urls]
        return {"success": True, "data": documents}

    def _extract_one(self, url: str, fmt: str, max_characters: Optional[int]) -> Dict[str, Any]:
        """One URL → one document. A failure is a document, never an exception:
        the other URLs in the same tool call are still worth returning."""
        config = self.config
        if not url.strip():
            return {"url": url, "title": "", "content": "", "raw_content": "", "error": "empty url"}
        body: Dict[str, Any] = {"model": config.fetch_model, "url": url.strip(), "format": fmt}
        if max_characters:
            body["max_characters"] = max_characters
        try:
            payload = unwrap(self._client().post("/web/fetch", body, config.fetch_timeout_s))
        except RouterError as exc:
            return {"url": url, "title": "", "content": "", "raw_content": "", "error": str(exc)}

        content = payload.get("content")
        text = ""
        if isinstance(content, dict):
            text = str(content.get("text") or "")
        elif isinstance(content, str):
            text = content
        if not text:
            text = str(payload.get("text") or payload.get("markdown") or "")
        if not text:
            return {
                "url": url,
                "title": str(payload.get("title") or ""),
                "content": "",
                "raw_content": "",
                "error": error_text(payload, "router returned no extractable content"),
            }
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        document: Dict[str, Any] = {
            "url": str(payload.get("url") or url),
            "title": str(payload.get("title") or ""),
            "content": text,
            # `raw_content` lets the model fall back to verbatim text; the router
            # already returns clean markdown, so both carry the same body.
            "raw_content": text,
        }
        if metadata:
            document["metadata"] = metadata
        return document

    # ── diagnostics (also reachable from the CLI / slash command) ──────────
    def status(self) -> Dict[str, Any]:
        config = self.config
        return {
            "provider": self.name,
            "configured": config.configured,
            "base_url": config.base_url,
            "auth": "api key" if config.api_key else "none",
            "search_model": config.search_model,
            "fetch_model": config.fetch_model,
            "search_timeout_s": config.search_timeout_s,
            "fetch_timeout_s": config.fetch_timeout_s,
            "max_characters": config.max_characters,
            "hermes_abc": "standalone stand-in" if STANDALONE else "agent.web_search_provider",
        }

    def doctor(self, live: bool = False) -> Dict[str, Any]:
        """Offline config check, plus one real search and one real extract when
        ``live`` is set. Live mode is opt-in because it spends the operator's
        search quota — the same reason `hermes doctor` asks first."""
        report: Dict[str, Any] = {"status": self.status(), "checks": []}
        if not self.is_available():
            report["checks"].append({"name": "config", "ok": False, "detail": "HERMES_ROUTER_URL is not set"})
            report["ok"] = False
            return report
        report["checks"].append({"name": "config", "ok": True, "detail": self.config.base_url})
        if not live:
            report["ok"] = True
            report["hint"] = "pass --live to run one real search and one real fetch"
            return report

        search = self.search("Hermes Router plugin doctor", limit=1)
        report["checks"].append(
            {
                "name": "search",
                "ok": bool(search.get("success")),
                "detail": (search.get("data", {}).get("web") or [{}])[0].get("url")
                if search.get("success")
                else search.get("error"),
            }
        )
        fetch = self.extract(["https://example.com"])
        document = (fetch.get("data") or [{}])[0]
        report["checks"].append(
            {
                "name": "fetch",
                "ok": bool(document.get("content")),
                "detail": (document.get("title") or document.get("url")) if document.get("content") else document.get("error"),
            }
        )
        report["ok"] = all(check["ok"] for check in report["checks"])
        return report
