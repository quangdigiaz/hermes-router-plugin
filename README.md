# Hermes Router — one plugin for inference + web search + web extract

> Installing this on behalf of a user? The agent-facing procedure is [`docs/HERMES_AGENT_PLUGIN.md`](docs/HERMES_AGENT_PLUGIN.md).

ONE install points Hermes Agent's **inference**, **`web_search`** and
**`web_extract`** at your Hermes Router: one URL, one key, one ledger.

| Surface | How it is served |
|---|---|
| **inference** | the router as a **model provider** — every model (`<provider>/<model>`) and combo (bare name) in its pool, with fail-over, health/cost ranking and one usage ledger. Hermes wires `--provider hermes-router`, the `/model` picker, `hermes doctor`, `hermes setup`, URL reverse-mapping and the auxiliary client from the declaration alone |
| `web_search` | `POST {HERMES_ROUTER_URL}/v1/search` — the router's own search pool (Tavily / Exa / Brave / Serper / SearXNG / Linkup / SearchAPI / You.com / Perplexity / fastCRW) with combo fail-over |
| `web_extract` | `POST {HERMES_ROUTER_URL}/v1/web/fetch` — fetch-conent (X/Twitter) / Jina Reader / Firecrawl / fastCRW / Tavily / Exa, already converted to clean markdown |
| Deep crawl | **not supported** — each requested URL is fetched once. Modes that ask for a crawl are ignored rather than silently answered with only the seed page. |

The agent needs no vendor keys at all: it needs the router URL, and (only when
your router requires auth) one router API key.

## Install — one command

```bash
hermes plugins install quangdigiaz/hermes-router-plugin --enable
```

That single install registers **both halves**. One manifest can only declare one
`kind`, and `backend` is the documented kind for a web provider — and the kind
whose loader *imports* the package. The import is the wiring: the model-provider
profile is registered at module level of
[`hermes_router_hermes/model_profile.py`](hermes_router_hermes/model_profile.py),
which is exactly how Hermes discovers out-of-tree providers. Nothing else to
install, and no second manifest to keep in sync.

Or pip:

```bash
pip install "git+https://github.com/quangdigiaz/hermes-router-plugin"
```

### Then configure (one export)

```bash
export HERMES_ROUTER_URL="http://localhost:20128"   # /v1 is added for you
export HERMES_ROUTER_KEY="sk-..."                   # only if requireApiKey=true
```

Both halves read that same pair — a second URL cannot silently desync web from
inference.

**Do you need `hermes config set`?** Usually not:

* **web** — when `web.search_backend` / `web.extract_backend` / `web.backend` are
  unset, Hermes auto-detects the backend from whichever URL/key is present. With
  only the router configured, the router wins. Set them explicitly to pin it
  against another provider you also have keys for.
* **inference** — `--provider hermes-router` (aliases `hermesrouter`,
  `hermes_router`) selects it per call, and `hermes model` shows its live
  catalogue. To make it the *default*, one line: `model.provider: hermes-router`
  in `config.yaml`.

`hermes hermes-router setup` prints exactly those lines, and
`hermes hermes-router status` shows what both halves resolved.

### If `--provider hermes-router` is not listed

Hermes reads an out-of-tree provider from the module-level
`register_provider(profile)` this plugin performs at import. On a build that
loads plugins *after* its first provider lookup, the profile can be missed for
that process. The documented fallback reads providers on the **next lookup, with
no restart** — and the file is self-contained by design (stdlib + `providers`
only, no package-relative imports; `tests/test_merged_plugin.py` pins that), so
it can be copied anywhere:

```bash
mkdir -p "$HERMES_HOME/plugins/model-providers/hermes-router"      # Windows: %LOCALAPPDATA%\hermes\plugins\...
cp hermes_router_hermes/model_profile.py \
   "$HERMES_HOME/plugins/model-providers/hermes-router/__init__.py"
```

Repeat per profile (`hermes -p <profile> …`) if you run several.

## Configure — environment

| Env var | Default | Meaning |
|---|---|---|
| `HERMES_ROUTER_URL` | — (required) | Base URL. `http://host:20128`, `…/` and `…/v1` all work. Serves inference, search, fetch and `/v1/models` |
| `HERMES_ROUTER_BASE_URL` | — | Same value under Hermes' base-URL-override name; wins if both are set |
| `HERMES_ROUTER_KEY` | none | Sent as `Authorization: Bearer …` on inference, search, fetch and the catalogue |
| `HERMES_ROUTER_AUX_MODEL` | unset | Cheap model id for auxiliary tasks (compression/titles/vision). Unset = Hermes' own fallback |
| `HERMES_ROUTER_MODELS` | unset | Comma-separated ids used only when the live catalogue fetch fails |
| `HERMES_ROUTER_SEARCH_MODEL` | `auto` | `auto` = router smart routing, or a provider/combo id from `/v1/models/web` |
| `HERMES_ROUTER_FETCH_MODEL` | `auto` | Same, for fetch (e.g. `jina-reader`, `fetch-combo`) |
| `HERMES_ROUTER_SEARCH_TIMEOUT_S` | `30` | Whole-request timeout for one search |
| `HERMES_ROUTER_FETCH_TIMEOUT_S` | `110` | Per-URL timeout — just under Hermes' own `web.extract_timeout` (120 s) so a slow page fails here, per URL, with a readable error |
| `HERMES_ROUTER_MAX_CHARACTERS` | router default | Hard cap on extracted text |

`is_available()` is offline by design (it runs on every `hermes tools` paint), so
nothing here makes a network call until a tool or a completion actually runs.

## Diagnose

```bash
hermes hermes-router status          # both halves: config, endpoints, model ids — no network
hermes hermes-router doctor          # offline checks for all three surfaces
hermes hermes-router doctor --live   # + one real search, one real fetch, one real catalogue fetch
hermes hermes-router model [--live]  # the inference profile + the ids the picker would show
hermes hermes-router search "hermes router web fetch"
hermes hermes-router fetch https://example.com --format markdown
```

In-session: `/hermes-router-status [live]`.

## Model discovery

`fetch_models()` calls `GET {base}/v1/models` and returns exactly what the router
lists: `<provider>/<model>` (a concrete model), a bare name (a **combo** — the
router's own fail-over chain, usually the right thing to pin in `config.yaml`),
and the router's `auto-router` templates. If the router is unreachable the fetch
returns `None` and Hermes falls back to `HERMES_ROUTER_MODELS` rather than showing
a wrong catalogue.

## Verify

```bash
python tests/test_provider.py       # offline: web envelopes, errors, register(ctx) — 24 tests
python tests/test_model_profile.py  # offline: the profile declaration + catalogue — 12 tests
python tests/test_merged_plugin.py  # offline: ONE install wires all three — 11 tests

HERMES_ROUTER_URL=http://localhost:20128 HERMES_ROUTER_KEY=sk-... \
  python tests/live_smoke.py        # live: search + extract + catalogue + one real completion
```

`live_smoke.py` exercises the interface Hermes itself uses — provider `search()`,
`extract()`, `register(ctx)`, the profile and its `fetch_models()` — against the
router you point it at, and prints the exact payloads. `--no-web` skips the two
web calls (quota), `--no-chat` skips the completion.

## Response envelopes (fixed by Hermes)

`search()` → `{"success": True, "data": {"web": [{"title","url","description","position"}]}}`
`extract()` → `{"success": True, "data": [{"url","title","content","raw_content","metadata"?,"error"?}]}`
failure → `{"success": False, "error": "<human sentence>"}`

A failed URL inside a multi-URL extract becomes an error document, so the other
URLs in the same tool call still come back.

## Limits (deliberate, not oversights)

* **No hardcoded model list.** A router's ids are per-install — a combo called
  `mimo` exists on one box and not the next — so the catalogue is discovered live
  and `HERMES_ROUTER_MODELS` covers an offline start.
* **No invented `default_aux_model`.** Set `HERMES_ROUTER_AUX_MODEL` explicitly.
* **Per-model capabilities are not declared.** Hermes reads
  `supports_reasoning` / `supports_vision` / `context_window` from models.dev,
  which does not know a router's ids. The provider-wide `supports_vision: true` is
  declared (the router proxies image content straight through); pin a specific
  model with a `model_overrides` entry in `config.yaml` if it must route images
  natively.
* **`/usage` stays empty.** The router's own ledger is dashboard-authenticated
  (behind the dashboard cookie), not callable with an API key, so no
  `fetch_account_usage` hook is shipped; watch costs in the router dashboard.
* **Deep crawl is not supported** (see the table above).
* **`max_tokens` → `max_characters`** uses a documented ≈4 chars/token
  approximation, because the router caps on characters.
* The router's own `429/402` answers surface as the tool's error text with the
  router's wording — the plugin does not retry, because the router already fails
  over between providers internally.
* **Restart Hermes after editing the profile** — the provider registry is
  discovered once per process (a new process picks the plugin up on first lookup).
